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

def debug_table_structure(page, max_lines=5):
    """Helper function to analyze table structure for debugging"""
    words = page.get_text("words")
    lines = defaultdict(list)
    
    for w in words:
        lines[round(w[1])].append(w)
    
    debug_info = []
    for y in sorted(lines.keys())[:max_lines]:
        line_words = sorted(lines[y], key=lambda w: w[0])
        positions = [f"{w[0]:.0f}" for w in line_words]
        texts = [w[4] for w in line_words]
        debug_info.append({
            'y_position': y,
            'x_positions': positions,
            'texts': texts
        })
    
    return debug_info

def align_columns_with_positions(line_words, measurement_pattern, expected_columns=7):
    """
    Align words to columns based on X-coordinate positions
    This helps handle null/missing columns properly
    """
    # Filter words that match measurement pattern
    filtered_words = [(w[0], w[4]) for w in line_words if measurement_pattern.match(w[4])]
    
    if len(filtered_words) < 5:  # Need at least 5 columns (no, dimension, upper, lower, pos)
        return None
    
    # Auto-detect column positions from the data
    # Sort by X position and try to identify column boundaries
    x_positions = [word[0] for word in filtered_words]
    x_positions.sort()
    
    # Define expected column positions based on typical table layouts
    # You may need to adjust these based on your specific PDF format
    if len(filtered_words) >= 6:
        # Try to auto-detect column positions
        step = (x_positions[-1] - x_positions[0]) / 6
        expected_positions = [x_positions[0] + i * step for i in range(7)]
    else:
        # Fallback to fixed positions - adjust these values based on your PDF
        expected_positions = [50, 120, 200, 280, 360, 440, 520]
    
    tolerance = 40  # Position tolerance - may need adjustment
    
    # Map words to columns based on X-position
    columns = [''] * expected_columns
    used_positions = set()
    
    for x_pos, text in filtered_words:
        best_col = None
        min_distance = float('inf')
        
        for i, expected_x in enumerate(expected_positions[:expected_columns]):
            distance = abs(x_pos - expected_x)
            if distance < tolerance and distance < min_distance and i not in used_positions:
                min_distance = distance
                best_col = i
        
        if best_col is not None:
            columns[best_col] = text
            used_positions.add(best_col)
    
    return columns if columns[0] and re.match(r'^\d+\.?\d*$', columns[0]) else None

def parse_measurement_entry_safe(columns):
    """
    Parse a measurement entry from column data, handling empty values safely
    """
    # Ensure we have at least 7 slots
    clean_columns = (columns + [''] * 7)[:7]
    
    no = clean_columns[0] if clean_columns[0] else ''
    
    # Check if second column is a symbol or dimension
    sym = ''
    dimension_index = 1
    
    if clean_columns[1] and not re.match(r'^[\d.-]+$', clean_columns[1]) and len(clean_columns[1]) <= 3:
        sym = clean_columns[1]
        dimension_index = 2
    
    # Extract remaining fields, handling empty values
    dimension = clean_columns[dimension_index] if len(clean_columns) > dimension_index else ''
    upper = clean_columns[dimension_index + 1] if len(clean_columns) > dimension_index + 1 else ''
    lower = clean_columns[dimension_index + 2] if len(clean_columns) > dimension_index + 2 else ''
    pos = clean_columns[dimension_index + 3] if len(clean_columns) > dimension_index + 3 else ''
    measured_by_vendor = clean_columns[dimension_index + 4] if len(clean_columns) > dimension_index + 4 else ''
    
    return {
        "no": no,
        "sym": sym,
        "dimension": dimension,
        "upper": upper,
        "lower": lower,
        "pos": pos,
        "measured_by_vendor": measured_by_vendor
    }

def extract_measurement_data_with_coords(page):
    """
    IMPROVED: Extract measurement data from PDF page with better null column handling
    """
    measurements = []
    words = page.get_text("words")
    lines = defaultdict(list)

    for w in words:
        lines[round(w[1])].append(w)

    measurement_char_pattern = re.compile(
        r"^(?:\d{1,3}(?:\.\d+)?|[A-Za-z]{1,3}|[⋟⋠⋡ΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩ℀℁ℂ℃℄℅℆ℇ℈℉ℊℋℌℍℎℏℐℑℒℓ℔ℕ№℗℘ℙℚℛℜℝ℞℟℠℡™℣ℤ℥Ω℧ⅎ⅏►◄▲▼ŴŶŸŹŻŽ]|[-−])$", 
        re.UNICODE
    )

    for y in sorted(lines.keys()):
        line_words = sorted(lines[y], key=lambda w: w[0])
        
        # Method 1: Try position-based alignment first
        aligned_columns = align_columns_with_positions(line_words, measurement_char_pattern)
        
        if aligned_columns:
            try:
                measurement = parse_measurement_entry_safe(aligned_columns)
                if measurement['no']:  # Only add if we have a valid number
                    measurements.append(measurement)
                continue
            except Exception:
                pass
        
        # Method 2: Fallback to original method with improvements
        text_line = [w[4] for w in line_words if measurement_char_pattern.match(w[4])]
        
        entries_to_process = []
        
        # Handle different line lengths more flexibly
        if 12 <= len(text_line) <= 16:  # Extended range to handle more variations
            split_point = len(text_line) // 2
            entries_to_process.append(text_line[:split_point])
            entries_to_process.append(text_line[split_point:])
        elif 5 <= len(text_line) <= 8:  # More flexible range
            entries_to_process.append(text_line)

        if entries_to_process:
            for entry_words in entries_to_process:
                try:
                    # Ensure we have enough elements
                    if len(entry_words) < 5:
                        continue
                        
                    # Pad with empty strings if needed
                    padded_words = (entry_words + [''] * 7)[:7]
                    
                    measurement = parse_measurement_entry_safe(padded_words)
                    if measurement['no']:  # Only add if we have a valid number
                        measurements.append(measurement)
                        
                except (IndexError, ValueError) as e:
                    # Log the error for debugging but continue processing
                    print(f"Warning: Could not parse measurement entry: {entry_words}, Error: {e}")
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
    
    # Extract measurements from other pages
    all_measurements = []
    debug_info = []
    
    for page_num in range(1, len(doc)):
        page = doc[page_num]
        measurements_on_page = extract_measurement_data_with_coords(page)
        if measurements_on_page:
            all_measurements.extend(measurements_on_page)
        
        # Add debug info for first few pages if needed
        if page_num <= 2:  # Debug first 2 measurement pages
            debug_info.append({
                'page': page_num,
                'structure': debug_table_structure(page, max_lines=3)
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
        "debug_info": debug_info  # Include debug info for troubleshooting
    }

@app.route('/api/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "service": "PDF Processing API - Vercel"
    })

@app.route('/api/debug-table/<cavity_id>', methods=['GET'])
def debug_table_endpoint(cavity_id):
    """Debug endpoint to analyze table structure"""
    # This would be used for debugging table structure issues
    return jsonify({
        "message": "Debug endpoint - implement with specific PDF data",
        "cavity_id": cavity_id
    })

@app.route('/api/process-pdf', methods=['POST'])
def process_pdf():
    """Process PDF files - optimized for Vercel with improved column detection"""
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
                    
                    # Process the PDF
                    result = process_pdf_data(pdf_data, filename)
                    
                    cavity_id = result["cavity_id"]
                    data_entry = {
                        "filename": filename,
                        "header_info": result["header_info"],
                        "measurements": result["measurements"],
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
            if empty_fields_count > len(measurements) * 0.1:  # More than 10% empty fields
                quality_warnings.append(f"{cavity_id}: High number of empty measurement fields detected")
        
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
