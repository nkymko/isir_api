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
        'supplier_name': r"Supplier name\s*(.*?)\s*Part No",
        'supplier_code': r"Supplier code No\.\s*(\S+)",
        'part_no': r"Part No\.\s*(\S+)",
        'part_name': r"Part name\s*(.*?)\s*Tooling No",
        'tooling_no': r"Tooling No\.\s*(\S+)",
        'cavity_no': r"Cavity No\.\s*(\S+)",
        'assy_name': r"ASSY \(SUB ASSY\) name\s*(.*?)\s*Material\s",
        'material': r"Material\s*(.*?)\s*Drawing standard",
        'material_manufacturer': r"Material manufacturer\s*(.*?)\s*Grade Name",
        'grade_name': r"Grade Name\s*(\S+)",
        'dds2004_result': r"Result:\s*\[\s*(YES|NO)\s*\]"
    }
    
    for key, pattern in patterns.items():
        match = re.search(pattern, page_text, re.DOTALL)
        if match:
            header_info[key] = match.group(1).strip().replace('\n', ' ')
        else:
            header_info[key] = None
            
    # RoHS data extraction
    rohs_data = {}
    rohs_patterns = {
        'cd_result': r"Cd\s*<0\.01%\s*(Not Detected)",
        'hg_result': r"Hg\s*<0\.1%\s*(Not Detected)",
        'pb_result': r"Pb\s*<0\.1%\s*(Not Detected)",
        'cr6_result': r"Cr 6\+\s*<0\.1%\s*(Not Detected)"
    }
    for key, pattern in rohs_patterns.items():
        match = re.search(pattern, page_text, re.DOTALL)
        if match:
            rohs_data[key] = match.group(1).strip()
        else:
            rohs_data[key] = None
    
    header_info['rohs_data'] = rohs_data
    return header_info

def get_text_at_coordinate(page, target_x, target_y, tolerance_x=15, tolerance_y=5):
    """
    Extract text near specific coordinates with given tolerance
    """
    words = page.get_text("words")
    found_texts = []
    
    for word in words:
        x0, y0, x1, y1, text, block_no, line_no, word_no = word
        word_x = x0  # Left edge of word
        word_y = y0  # Top edge of word
        
        # Check if word is within tolerance of target coordinates
        if (abs(word_x - target_x) <= tolerance_x and 
            abs(word_y - target_y) <= tolerance_y):
            found_texts.append({
                'text': text,
                'x': word_x,
                'y': word_y,
                'distance': ((word_x - target_x)**2 + (word_y - target_y)**2)**0.5
            })
    
    # Return the closest text if any found
    if found_texts:
        found_texts.sort(key=lambda x: x['distance'])
        return found_texts[0]['text']
    
    return ""

def extract_measurement_data_by_coordinates(page, start_y=182.85, row_height=12, num_rows=43):
    """
    Extract measurement data using fixed coordinates for your specific PDF layout
    """
    measurements = []
    
    # Define column coordinates for left and right layouts
    left_columns = {
        'no': 55.71,
        'sym': 75.71, 
        'dimension': 105.28,
        'upper': 136.19,
        'lower': 162.38,
        'pos': 185.71,
        'measured_by_vendor': 220
    }
    
    right_columns = {
        'no': 323.33,
        'sym': 344.28,
        'dimension': 373.80, 
        'upper': 407.14,
        'lower': 430.95,
        'pos': 456.19,
        'measured_by_vendor': 487.61
    }
    
    tolerance_x = 15  # Horizontal tolerance for finding text
    tolerance_y = 6   # Vertical tolerance for finding text
    
    for row in range(num_rows):
        current_y = start_y + (row * row_height)
        
        # Extract left side data
        left_data = {}
        for field, x_coord in left_columns.items():
            text = get_text_at_coordinate(page, x_coord, current_y, tolerance_x, tolerance_y)
            left_data[field] = text.strip() if text else ""
        
        # Extract right side data
        right_data = {}
        for field, x_coord in right_columns.items():
            text = get_text_at_coordinate(page, x_coord, current_y, tolerance_x, tolerance_y)
            right_data[field] = text.strip() if text else ""
        
        # Add left side measurement if it has a valid number
        if left_data['no'] and re.match(r'^\d+\.?\d*$', left_data['no']):
            measurements.append({
                "no": left_data['no'],
                "sym": left_data['sym'],
                "dimension": left_data['dimension'],
                "upper": left_data['upper'], 
                "lower": left_data['lower'],
                "pos": left_data['pos'],
                "measured_by_vendor": left_data['measured_by_vendor'],
                "side": "left",
                "row": row + 1
            })
        
        # Add right side measurement if it has a valid number  
        if right_data['no'] and re.match(r'^\d+\.?\d*$', right_data['no']):
            measurements.append({
                "no": right_data['no'],
                "sym": right_data['sym'], 
                "dimension": right_data['dimension'],
                "upper": right_data['upper'],
                "lower": right_data['lower'], 
                "pos": right_data['pos'],
                "measured_by_vendor": right_data['measured_by_vendor'],
                "side": "right",
                "row": row + 1
            })
    
    return measurements

def debug_coordinate_extraction(page, start_y=182.85, num_debug_rows=5):
    """
    Debug function to show what text is found at each coordinate
    """
    debug_info = []
    
    left_columns = {
        'no': 55.71,
        'sym': 75.71,
        'dimension': 105.28, 
        'upper': 136.19,
        'lower': 162.38,
        'pos': 185.71,
        'measured_by_vendor': 220
    }
    
    right_columns = {
        'no': 323.33,
        'sym': 344.28,
        'dimension': 373.80,
        'upper': 407.14,
        'lower': 430.95,
        'pos': 456.19,
        'measured_by_vendor': 487.61
    }
    
    for row in range(num_debug_rows):
        current_y = start_y + (row * 12)  # Assuming 12pt row height
        
        row_debug = {
            'row': row + 1,
            'y_position': current_y,
            'left_side': {},
            'right_side': {}
        }
        
        # Debug left side
        for field, x_coord in left_columns.items():
            text = get_text_at_coordinate(page, x_coord, current_y, 15, 6)
            row_debug['left_side'][field] = {
                'coordinate': f"({x_coord}, {current_y})",
                'found_text': text
            }
        
        # Debug right side  
        for field, x_coord in right_columns.items():
            text = get_text_at_coordinate(page, x_coord, current_y, 15, 6)
            row_debug['right_side'][field] = {
                'coordinate': f"({x_coord}, {current_y})",
                'found_text': text
            }
        
        debug_info.append(row_debug)
    
    return debug_info

def extract_cavity_number_from_filename(filename):
    """Extract cavity number from filename"""
    match = re.search(r'CAV-(\d+)', filename, re.IGNORECASE)
    if match:
        return f"CAV-{match.group(1)}"
    else:
        return os.path.splitext(os.path.basename(filename))[0]

def process_pdf_data(pdf_data, filename):
    """Process PDF data from memory using coordinate-based extraction"""
    try:
        doc = fitz.open(stream=pdf_data, filetype="pdf")
    except Exception as e:
        raise Exception(f"Error opening PDF: {str(e)}")

    cavity_id = extract_cavity_number_from_filename(filename)
    
    # Extract header info from first page
    first_page_text = doc[0].get_text("text")
    header_data = extract_header_data(first_page_text)
    
    # Extract measurements from measurement pages using coordinates
    all_measurements = []
    debug_info = []
    
    # Start from page 1 (second page, index 1) for measurements
    for page_num in range(1, len(doc)):
        page = doc[page_num]
        
        # Extract measurements using coordinate-based method
        measurements_on_page = extract_measurement_data_by_coordinates(page)
        if measurements_on_page:
            all_measurements.extend(measurements_on_page)
        
        # Add debug info for first measurement page
        if page_num == 1:
            debug_info.append({
                'page': page_num,
                'coordinate_debug': debug_coordinate_extraction(page)
            })

    # Remove duplicates and sort
    unique_measurements = []
    seen = set()
    for measurement in all_measurements:
        # Create a signature for duplicate detection
        signature = (measurement['no'], measurement['sym'], measurement['dimension'])
        if signature not in seen:
            seen.add(signature)
            unique_measurements.append(measurement)
    
    # Sort by measurement number
    unique_measurements.sort(key=lambda x: int(x['no']) if x['no'].isdigit() else 0)
    
    doc.close()
    
    return {
        "cavity_id": cavity_id,
        "header_info": header_data,
        "measurements": unique_measurements,
        "debug_info": debug_info
    }

@app.route('/api/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "service": "PDF Processing API - Vercel (Coordinate-based)"
    })

@app.route('/api/debug-coordinates/<cavity_id>', methods=['POST'])
def debug_coordinates_endpoint(cavity_id):
    """Debug endpoint to analyze coordinate extraction"""
    try:
        if 'file' not in request.files:
            return jsonify({"error": "No file provided"}), 400
        
        file = request.files['file']
        if not file or not allowed_file(file.filename):
            return jsonify({"error": "Invalid file"}), 400
        
        pdf_data = file.read()
        doc = fitz.open(stream=pdf_data, filetype="pdf")
        
        if len(doc) < 2:
            return jsonify({"error": "PDF must have at least 2 pages"}), 400
        
        # Debug first measurement page
        page = doc[1]  # Second page (index 1)
        debug_data = debug_coordinate_extraction(page, num_debug_rows=10)
        
        doc.close()
        
        return jsonify({
            "cavity_id": cavity_id,
            "debug_data": debug_data,
            "message": "Coordinate extraction debug information"
        })
        
    except Exception as e:
        return jsonify({
            "error": "Debug failed",
            "message": str(e)
        }), 500

@app.route('/api/process-pdf', methods=['POST'])
def process_pdf():
    """Process PDF files using coordinate-based extraction"""
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
        debug_mode = request.form.get('debug', 'false').lower() == 'true'
        
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
                    
                    # Process the PDF using coordinate-based method
                    result = process_pdf_data(pdf_data, filename)
                    
                    cavity_id = result["cavity_id"]
                    data_entry = {
                        "filename": filename,
                        "header_info": result["header_info"],
                        "measurements": result["measurements"],
                        "extraction_method": "coordinate-based",
                        "processed_at": datetime.now().isoformat()
                    }
                    
                    # Include debug info if requested
                    if debug_mode:
                        data_entry["debug_info"] = result["debug_info"]
                    
                    all_data[cavity_id] = data_entry
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
        
        # Check for potential data quality issues
        quality_warnings = []
        for cavity_id, data in all_data.items():
            measurements = data['measurements']
            empty_fields_count = sum(1 for m in measurements 
                                   for field in ['dimension', 'upper', 'lower', 'pos'] 
                                   if not m.get(field, '').strip())
            if measurements and empty_fields_count > len(measurements) * 0.1:  # More than 10% empty fields
                quality_warnings.append(f"{cavity_id}: High number of empty measurement fields detected")
        
        response_data = {
            "success": True,
            "summary": {
                "files_processed": successful_extractions,
                "total_files": len(files),
                "cavities_found": list(all_data.keys()),
                "total_measurements": total_measurements,
                "extraction_method": "coordinate-based",
                "processed_at": datetime.now().isoformat()
            },
            "data": all_data
        }
        
        if errors:
            response_data["warnings"] = errors
            
        if quality_warnings:
            response_data["quality_warnings"] = quality_warnings
        
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
