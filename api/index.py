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

def _is_numeric_value(text):
    """Check if text represents a numeric value (including decimals, negatives, etc.)"""
    if not text:
        return False
    try:
        # Clean the text first
        clean_text = text.replace('+', '').replace(',', '.').strip()
        # Handle negative values
        if clean_text.startswith('-'):
            clean_text = clean_text[1:]
        float(clean_text)
        return True
    except ValueError:
        return False

def _parse_measurement_parts(parts):
    """
    Parse measurement from a list of parts using flexible, right-to-left logic.
    This is more robust against missing columns like 'Sym.' or 'Pos.'.
    """
    if len(parts) < 2:  # Must have at least a number and a measurement value
        return None

    # Initialize the dictionary to store results
    measurement = {
        "no": parts[0],
        "sym": "",
        "dimension": "",
        "upper": "",
        "lower": "",
        "pos": "",
        "measured_by_vendor": ""
    }

    # Use a copy of the parts, excluding the 'No.'
    remaining_parts = parts[1:]

    # 1. Get 'measured_by_vendor' (last item, if numeric)
    if remaining_parts and _is_numeric_value(remaining_parts[-1]):
        measurement['measured_by_vendor'] = remaining_parts.pop(-1)

    # 2. Get 'pos' (the new last item, if it's NOT numeric)
    if remaining_parts and not _is_numeric_value(remaining_parts[-1]):
        measurement['pos'] = remaining_parts.pop(-1)

    # 3. Process the rest (Sym, Dimension, Upper, Lower) from left to right
    # Check for a 'Sym' (Symbol) which is typically non-numeric
    if remaining_parts and not _is_numeric_value(remaining_parts[0]):
        measurement['sym'] = remaining_parts.pop(0)

    # Assign the rest in order
    if len(remaining_parts) > 0:
        measurement['dimension'] = remaining_parts.pop(0)
    if len(remaining_parts) > 0:
        measurement['upper'] = remaining_parts.pop(0)
    if len(remaining_parts) > 0:
        measurement['lower'] = remaining_parts.pop(0)

    # Only return a valid measurement if the vendor value was found
    if measurement['measured_by_vendor']:
        return measurement
    
    return None

def _parse_measurement_parts(parts):
    """Parse measurement from parts array"""
    if len(parts) < 4:  # Minimum: no, dimension, some values
        return None
    
    try:
        no = parts[0]
        if not no.isdigit():
            return None
        
        # Check for symbol
        sym = ""
        idx = 1
        
        # Common symbols to look for
        symbol_indicators = ['Ø', 'ø', 'BURR', '⌀', 'φ']
        if idx < len(parts) and any(symbol in parts[idx] for symbol in symbol_indicators):
            sym = parts[idx]
            idx += 1
        elif idx < len(parts) and not _is_numeric_value(parts[idx]):
            # If it's not numeric, it might be a symbol
            sym = parts[idx]
            idx += 1
        
        # Extract remaining fields with defaults
        dimension = parts[idx] if idx < len(parts) else ""
        upper = parts[idx + 1] if idx + 1 < len(parts) else ""
        lower = parts[idx + 2] if idx + 2 < len(parts) else ""
        pos = parts[idx + 3] if idx + 3 < len(parts) else ""
        measured_by_vendor = parts[idx + 4] if idx + 4 < len(parts) else ""
        
        return {
            "no": no,
            "sym": sym,
            "dimension": dimension,
            "upper": upper,
            "lower": lower,
            "pos": pos,
            "measured_by_vendor": measured_by_vendor
        }
    except:
        return None

def _extract_from_words(words):
    """Extract measurements by analyzing word positions"""
    measurements = []
    
    # Group words by approximate line (y-coordinate)
    lines = defaultdict(list)
    for word in words:
        y_coord = round(word[1], -1)  # Round to nearest 10 pixels
        lines[y_coord].append(word)
    
    # Sort each line by x-coordinate
    for y_coord in lines:
        lines[y_coord].sort(key=lambda w: w[0])
    
    # Process each line
    for y_coord in sorted(lines.keys()):
        line_words = [w[4] for w in lines[y_coord]]  # Extract text
        line_text = ' '.join(line_words)
        
        # Skip header lines
        if any(header in line_text.upper() for header in ['NO.', 'SYM.', 'DIMENSION', 'UPPER', 'LOWER', 'POS.', 'INDICATE']):
            continue
        
        # Try to parse measurement from this line
        if line_words and line_words[0].isdigit():
            try:
                measurement = _parse_measurement_parts(line_words)
                if measurement:
                    measurements.append(measurement)
            except:
                continue
    
    return measurements

def extract_measurement_data_with_coords(page):
    """Extract measurement data from PDF page - Fixed version"""
    measurements = []
    
    # Method 1: Try extracting from blocks
    blocks = page.get_text("blocks")
    
    for block in blocks:
        if isinstance(block, tuple) and len(block) >= 5:
            block_text = block[4]  # Text content
            
            # Parse measurements from this block
            block_measurements = _parse_measurements_from_text(block_text)
            measurements.extend(block_measurements)
    
    # Method 2: If no measurements found, try parsing from raw text
    if not measurements:
        page_text = page.get_text("text")
        measurements = _parse_measurements_from_text(page_text)
    
    # Method 3: If still no measurements, try word-by-word analysis
    if not measurements:
        words = page.get_text("words")
        measurements = _extract_from_words(words)
    
    # Additional method: Try to find table-like structures
    if not measurements:
        measurements = _extract_table_data(page)
    
    return measurements

def _extract_table_data(page):
    """Try to extract data assuming it's in a table format"""
    measurements = []
    
    # Get text with detailed positioning
    text_dict = page.get_text("dict")
    
    # Look for potential table rows
    for block in text_dict.get("blocks", []):
        if "lines" not in block:
            continue
            
        for line in block["lines"]:
            line_text = ""
            for span in line.get("spans", []):
                line_text += span.get("text", "") + " "
            
            line_text = line_text.strip()
            if not line_text:
                continue
            
            # Parse potential measurement line
            parts = line_text.split()
            if parts and parts[0].isdigit():
                measurement = _parse_measurement_parts(parts)
                if measurement:
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
    """Process PDF data from memory - Updated version"""
    try:
        doc = fitz.open(stream=pdf_data, filetype="pdf")
    except Exception as e:
        raise Exception(f"Error opening PDF: {str(e)}")

    cavity_id = extract_cavity_number_from_filename(filename)
    
    # Extract header info from first page
    first_page_text = doc[0].get_text("text")
    header_data = extract_header_data(first_page_text)
    
    # Extract measurements from ALL pages
    all_measurements = []
    for page_num in range(len(doc)):
        page = doc[page_num]
        measurements_on_page = extract_measurement_data_with_coords(page)
        if measurements_on_page:
            # Add page number for debugging
            for measurement in measurements_on_page:
                measurement['page'] = page_num
            all_measurements.extend(measurements_on_page)

    # Remove duplicates and sort
    unique_measurements = []
    seen = set()
    for measurement in all_measurements:
        # Create a tuple of key values for deduplication
        key = (measurement['no'], measurement['dimension'])
        if key not in seen:
            seen.add(key)
            unique_measurements.append(measurement)
    
    # Sort by measurement number
    unique_measurements.sort(key=lambda x: int(x['no']) if x['no'].isdigit() else 0)
    
    doc.close()
    
    return {
        "cavity_id": cavity_id,
        "header_info": header_data,
        "measurements": unique_measurements
    }

@app.route('/api/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "service": "PDF Processing API - Vercel"
    })

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
# Remove the incorrect handler function and use this instead:
app = app  # Export the Flask app directly

if __name__ == '__main__':
    app.run(debug=True)
