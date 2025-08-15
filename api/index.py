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
    """Improved measurement data extraction with better text parsing"""
    measurements = []
    
    # Get all text with positions
    text_dict = page.get_text("dict")
    
    # Also try direct text extraction to find the table
    page_text = page.get_text()
    
    # Look for measurement table patterns in the text
    # Pattern 1: Look for numbered rows with dimensions
    lines = page_text.split('\n')
    
    measurement_lines = []
    for i, line in enumerate(lines):
        line = line.strip()
        # Look for lines that start with a number and contain measurement data
        if re.match(r'^\d+\s+', line):
            measurement_lines.append(line)
    
    # Parse each measurement line
    for line in measurement_lines:
        parts = re.split(r'\s+', line.strip())
        if len(parts) >= 4:
            try:
                no = parts[0]
                
                # Find dimension value (look for numbers)
                dimension = None
                upper = None
                lower = None
                measured_by_vendor = None
                sym = ""
                
                # Simple parsing for the example data
                if len(parts) >= 6:
                    dimension = parts[1]
                    upper = parts[2] if parts[2] not in ['0', '-'] else None
                    lower = parts[3] if parts[3] not in ['0', '-'] else None
                    # Look for the measured value (usually the last numeric value)
                    for part in parts[4:]:
                        if re.match(r'^\d+\.?\d*$', part):
                            measured_by_vendor = part
                            break
                
                if dimension:
                    measurements.append({
                        "no": no,
                        "sym": sym,
                        "dimension": dimension,
                        "upper": upper,
                        "lower": lower,
                        "pos": "",
                        "measured_by_vendor": measured_by_vendor or ""
                    })
                    
            except (IndexError, ValueError):
                continue
    
    # Alternative approach: Extract from the actual table structure
    if not measurements:
        # Look for table-like structures in the PDF
        words = page.get_text("words")
        
        # Group words by approximate Y position (rows)
        lines_dict = defaultdict(list)
        for word in words:
            y_pos = round(word[1], 0)  # Round Y position to group by lines
            lines_dict[y_pos].append(word)
        
        # Sort lines by Y position
        sorted_lines = sorted(lines_dict.items())
        
        for y_pos, word_list in sorted_lines:
            # Sort words in line by X position
            word_list.sort(key=lambda w: w[0])
            line_text = ' '.join([w[4] for w in word_list])
            
            # Check if this looks like a measurement row
            if re.match(r'^\d+\s+', line_text):
                parts = line_text.split()
                if len(parts) >= 3:
                    try:
                        measurements.append({
                            "no": parts[0],
                            "sym": "",
                            "dimension": parts[1] if len(parts) > 1 else "",
                            "upper": parts[2] if len(parts) > 2 else "",
                            "lower": parts[3] if len(parts) > 3 else "",
                            "pos": parts[4] if len(parts) > 4 else "",
                            "measured_by_vendor": parts[5] if len(parts) > 5 else ""
                        })
                    except:
                        continue
    
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

def debug_pdf_structure(pdf_data):
    """Debug function to understand PDF structure"""
    doc = fitz.open(stream=pdf_data, filetype="pdf")
    
    for page_num in range(len(doc)):
        print(f"\n=== PAGE {page_num + 1} ===")
        page = doc[page_num]
        
        # Print raw text
        text = page.get_text()
        print("Raw text:")
        print(text)
        
        # Print words with positions
        words = page.get_text("words")
        print(f"\nFound {len(words)} words")
        for i, word in enumerate(words[:10]):  # First 10 words
            print(f"Word {i}: {word}")
    
    doc.close()

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
