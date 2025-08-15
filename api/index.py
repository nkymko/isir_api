from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import fitz  # PyMuPDF
import json
import re
import pandas as pd
from collections import defaultdict
import os
import tempfile
from werkzeug.utils import secure_filename
import io
from datetime import datetime

app = Flask(__name__)

CORS(app)

@app.route('/')
def home():
    return 'Hello, World!'

# Vercel serverless configuration
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024  # Reduced to 10MB for Vercel

ALLOWED_EXTENSIONS = {'pdf'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def extract_header_data(page_text):
    """Extract header information from the text of the first page"""
    header_info = {}
    patterns = {
        'supplier_name': r"Supplier name\s*(.*?)\s*(?:Part No|$)",
        'supplier_code': r"Supplier code No\.\s*(\S+)",
        'part_no': r"Part No\.\s*(\S+)",
        'part_name': r"Part name\s*(.*?)\s*(?:Tooling No|$)",
        'tooling_no': r"Tooling No\.?\s*(\S+)",
        'cavity_no': r"Cavity No\.?\s*(\S+)",
        'assy_name': r"ASSY \(SUB ASSY\) name\s*(.*?)\s*(?:Material|$)",
        'material': r"Material\s*(.*?)\s*(?:Drawing standard|Material manufacturer|$)",
        'material_manufacturer': r"Material manufacturer\s*(.*?)\s*(?:Grade Name|$)",
        'grade_name': r"Grade Name\s*(\S+)",
        'dds2004_result': r"Result:\s*\[\s*(YES|NO)\s*\]"
    }
    
    for key, pattern in patterns.items():
        match = re.search(pattern, page_text, re.DOTALL | re.IGNORECASE)
        if match:
            header_info[key] = match.group(1).strip().replace('\n', ' ')
        else:
            header_info[key] = None
            
    # RoHS data extraction - Fixed patterns
    rohs_data = {}
    rohs_patterns = {
        'cd_result': r"Cd\s*<0\.01%\s*(Not Detected|Detected)",
        'hg_result': r"Hg\s*<0\.1%\s*(Not Detected|Detected)", 
        'pb_result': r"Pb\s*<0\.1%\s*(Not Detected|Detected)",
        'cr6_result': r"Cr\s*6\+?\s*<0\.1%\s*(Not Detected|Detected)"
    }
    for key, pattern in rohs_patterns.items():
        match = re.search(pattern, page_text, re.DOTALL | re.IGNORECASE)
        if match:
            rohs_data[key] = match.group(1).strip()
        else:
            rohs_data[key] = None
    
    header_info['rohs_data'] = rohs_data
    return header_info

def extract_measurement_data_improved(page):
    """Improved measurement data extraction with better table structure detection"""
    measurements = []
    
    # Get page text
    page_text = page.get_text()
    
    # First, try to find the table headers to understand column positions
    header_pattern = r'No\.\s+Sym\.\s+Dimension\s+Upper\s+Lower\s+Pos\.\s+.*(?:Measured|by)'
    header_match = re.search(header_pattern, page_text, re.IGNORECASE)
    
    if not header_match:
        print("Could not find table headers")
        return extract_measurement_data_fallback(page)
    
    # Get text with word positions for better parsing
    words = page.get_text("words")
    
    # Group words by Y position (rows) with some tolerance
    rows = defaultdict(list)
    for word in words:
        x, y0, x1, y1, text, block_no, line_no, word_no = word
        # Round Y position to group words in same row
        row_key = round(y0 / 2) * 2  # Group with 2-point tolerance
        rows[row_key].append({
            'x': x,
            'text': text,
            'word_data': word
        })
    
    # Sort rows by Y position
    sorted_rows = sorted(rows.items())
    
    # Find header row to establish column positions
    header_row = None
    header_y = None
    
    for y_pos, word_list in sorted_rows:
        row_text = ' '.join([w['text'] for w in sorted(word_list, key=lambda x: x['x'])])
        if re.search(r'No\.\s+Sym\.\s+Dimension', row_text, re.IGNORECASE):
            header_row = word_list
            header_y = y_pos
            break
    
    if not header_row:
        print("Could not find header row")
        return extract_measurement_data_fallback(page)
    
    # Establish column positions from header
    header_sorted = sorted(header_row, key=lambda x: x['x'])
    column_positions = {}
    
    for word in header_sorted:
        text = word['text'].lower().strip('.')
        if 'no' in text:
            column_positions['no'] = word['x']
        elif 'sym' in text:
            column_positions['sym'] = word['x']
        elif 'dimension' in text:
            column_positions['dimension'] = word['x']
        elif 'upper' in text:
            column_positions['upper'] = word['x']
        elif 'lower' in text:
            column_positions['lower'] = word['x']
        elif 'pos' in text:
            column_positions['pos'] = word['x']
        elif 'measured' in text or 'vendor' in text:
            column_positions['measured_by_vendor'] = word['x']
    
    print(f"Found column positions: {column_positions}")
    
    # Process data rows (after header)
    for y_pos, word_list in sorted_rows:
        if y_pos <= header_y:
            continue  # Skip header and above
            
        # Sort words in row by X position
        row_words = sorted(word_list, key=lambda x: x['x'])
        row_text = ' '.join([w['text'] for w in row_words])
        
        # Check if this looks like a data row (starts with number)
        if not re.match(r'^\d+', row_text.strip()):
            continue
            
        # Extract data by assigning words to columns based on position
        measurement = {
            "no": "",
            "sym": "",
            "dimension": "",
            "upper": "",
            "lower": "",
            "pos": "",
            "measured_by_vendor": ""
        }
        
        # Assign each word to the closest column
        for word in row_words:
            word_x = word['x']
            word_text = word['text'].strip()
            
            if not word_text:
                continue
                
            # Find the closest column
            closest_column = None
            min_distance = float('inf')
            
            for col_name, col_x in column_positions.items():
                distance = abs(word_x - col_x)
                if distance < min_distance:
                    min_distance = distance
                    closest_column = col_name
            
            # Assign to closest column if within reasonable distance
            if closest_column and min_distance < 50:  # 50 point tolerance
                if measurement[closest_column]:  # If column already has value, append
                    measurement[closest_column] += " " + word_text
                else:
                    measurement[closest_column] = word_text
        
        # Only add if we have at least a number and dimension
        if measurement["no"] and measurement["dimension"]:
            measurements.append(measurement)
            print(f"Extracted: {measurement}")
    
    return measurements


def extract_measurement_data_fallback(page):
    """Fallback method using regex patterns"""
    measurements = []
    page_text = page.get_text()
    
    # Try to extract table section
    table_match = re.search(r'No\.\s+Sym\..*?(?=Notes|$)', page_text, re.DOTALL | re.IGNORECASE)
    if not table_match:
        return measurements
    
    table_text = table_match.group(0)
    lines = table_text.split('\n')
    
    for line in lines:
        line = line.strip()
        # Look for lines starting with a number
        if re.match(r'^\d+\s', line):
            # More flexible parsing
            parts = re.split(r'\s+', line)
            if len(parts) >= 2:
                measurement = {
                    "no": parts[0],
                    "sym": "",
                    "dimension": "",
                    "upper": "",
                    "lower": "",
                    "pos": "",
                    "measured_by_vendor": ""
                }
                
                # Try to identify parts by patterns
                for i, part in enumerate(parts[1:], 1):
                    if i == 1 and not re.match(r'^[\d\.\+\-]+$', part):
                        measurement["sym"] = part
                    elif re.match(r'^\d+\.?\d*$', part) and not measurement["dimension"]:
                        measurement["dimension"] = part
                    elif re.match(r'^[\+\-]\d+\.?\d*$', part):
                        if not measurement["upper"]:
                            measurement["upper"] = part
                        elif not measurement["lower"]:
                            measurement["lower"] = part
                    elif re.match(r'^\d+\.?\d*$', part) and measurement["dimension"]:
                        measurement["measured_by_vendor"] = part
                
                measurements.append(measurement)
    
    return measurements


def extract_cavity_number_from_filename(filename):
    """Extract cavity number from filename"""
    match = re.search(r'CAV-(\d+)', filename, re.IGNORECASE)
    if match:
        return f"CAV-{match.group(1)}"
    else:
        return os.path.splitext(os.path.basename(filename))[0]

def process_pdf_data(pdf_data, filename):
    """Process PDF data from memory"""
    try:
        doc = fitz.open(stream=pdf_data, filetype="pdf")
    except Exception as e:
        raise Exception(f"Error opening PDF: {str(e)}")

    cavity_id = extract_cavity_number_from_filename(filename)
    
    # Extract header info from first page
    first_page_text = doc[0].get_text("text")
    header_data = extract_header_data(first_page_text)
    
    # Debug: Print the extracted text to see what we're working with
    print(f"First page text preview: {first_page_text[:500]}...")
    
    # Extract measurements from all pages (including first page)
    all_measurements = []
    for page_num in range(len(doc)):
        page = doc[page_num]
        measurements_on_page = extract_measurement_data_improved(page)
        if measurements_on_page:
            all_measurements.extend(measurements_on_page)
        
        # Debug: Print page text to see structure
        page_text = page.get_text()
        print(f"Page {page_num} text preview: {page_text[:200]}...")

    # Remove duplicates and sort
    unique_measurements = []
    seen = set()
    for measurement in all_measurements:
        # Create a tuple of the measurement for deduplication
        measurement_tuple = tuple(measurement.items())
        if measurement_tuple not in seen:
            seen.add(measurement_tuple)
            unique_measurements.append(measurement)
    
    # Sort by number
    unique_measurements.sort(key=lambda x: int(x['no']) if x['no'].isdigit() else 0)
    
    doc.close()
    
    return {
        "cavity_id": cavity_id,
        "header_info": header_data,
        "measurements": unique_measurements
    }

def debug_pdf_table_structure(page):
    """Enhanced debug function to understand table structure"""
    print("\n=== DEBUGGING TABLE STRUCTURE ===")
    
    # Print raw text with line numbers
    text = page.get_text()
    lines = text.split('\n')
    for i, line in enumerate(lines):
        if 'No.' in line or re.match(r'^\d+\s', line.strip()):
            print(f"Line {i}: '{line}'")
    
    # Print words with positions
    words = page.get_text("words")
    print(f"\n=== WORDS WITH POSITIONS ===")
    
    # Group words by approximate Y position
    rows = defaultdict(list)
    for word in words:
        x, y0, x1, y1, text, block_no, line_no, word_no = word
        row_key = round(y0 / 2) * 2
        rows[row_key].append((x, text))
    
    # Show a few rows around the table
    sorted_rows = sorted(rows.items())
    for y_pos, word_list in sorted_rows:
        row_text = ' '.join([w[1] for w in sorted(word_list, key=lambda x: x[0])])
        if 'No.' in row_text or re.match(r'^\d+', row_text.strip()):
            print(f"Y={y_pos}: {[(x, text) for x, text in sorted(word_list, key=lambda x: x[0])]}")
            print(f"   Text: '{row_text}'")

@app.route('/api/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "service": "PDF Processing API - Vercel"
    })

@app.route('/api/debug-pdf', methods=['POST'])
def debug_pdf():
    """Debug endpoint to understand PDF structure"""
    if 'file' not in request.files:
        return jsonify({"error": "No file provided"}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No file selected"}), 400
    
    try:
        pdf_data = file.read()
        debug_pdf_structure(pdf_data)
        return jsonify({"message": "Debug output printed to console"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/process-pdf', methods=['POST'])
def process_pdf():
    """Process PDF files - optimized for Vercel"""
    try:
        # Check for files
        if 'files' not in request.files:
            return jsonify({
                "error": "No files provided",
                "message": "Please upload PDF files using the 'files' field"
            }), 400
        
        files = request.files.getlist('files')
        
        if not files or all(f.filename == '' for f in files):
            return jsonify({
                "error": "No files selected"
            }), 400
        
        all_data = {}
        successful_extractions = 0
        errors = []
        
        for file in files:
            if file and file.filename != '':
                filename = secure_filename(file.filename)
                
                if not allowed_file(filename):
                    errors.append(f"{filename}: Invalid file type")
                    continue
                
                try:
                    # Read file data into memory
                    pdf_data = file.read()
                    
                    # Check file size (Vercel has memory limits)
                    if len(pdf_data) > 10 * 1024 * 1024:  # 10MB
                        errors.append(f"{filename}: File too large (>10MB)")
                        continue
                    
                    # Process the PDF
                    result = process_pdf_data(pdf_data, filename)
                    
                    cavity_id = result["cavity_id"]
                    all_data[cavity_id] = {
                        "filename": filename,
                        "header_info": result["header_info"],
                        "measurements": result["measurements"],
                        "processed_at": datetime.now().isoformat()
                    }
                    successful_extractions += 1
                    
                except Exception as e:
                    errors.append(f"{filename}: {str(e)}")
                    continue
        
        if successful_extractions == 0:
            return jsonify({
                "error": "No files were successfully processed",
                "errors": errors
            }), 400
        
        # Calculate summary
        total_measurements = sum(len(data['measurements']) for data in all_data.values())
        
        response_data = {
            "success": True,
            "summary": {
                "files_processed": successful_extractions,
                "total_files": len(files),
                "cavities_found": list(all_data.keys()),
                "total_measurements": total_measurements,
                "processed_at": datetime.now().isoformat()
            },
            "data": all_data
        }
        
        if errors:
            response_data["warnings"] = errors
        
        return jsonify(response_data)
        
    except Exception as e:
        return jsonify({
            "error": "Internal server error",
            "message": str(e)
        }), 500

# CORRECTED: Proper Vercel export
app = app  # Export the Flask app directly

if __name__ == '__main__':
    app.run(debug=True)
