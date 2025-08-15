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

def extract_measurement_data_with_coords(page):
    """Extract measurement data from PDF page - Ultra simple regex approach"""
    measurements = []
    
    # Get the raw text from the page
    page_text = page.get_text()
    
    # Based on your PDF, I can see these exact patterns:
    # 1 Ø 10 +0.012 -0.5 Bottom 9.89
    # 2 5 0 -0.01 4.99
    # 3 2 +0.02 +0.01 2.002
    # 4 4 -0,05 -0,1 4
    # 5 BURR 0.1 0.05
    # 6 11 0.3 11.1
    
    # Let's use specific regex patterns for each type
    patterns = [
        # Pattern 1: No Sym Dimension Upper Lower Pos MeasuredValue
        r'(\d+)\s+(Ø|BURR)\s+([\d.,+-]+)\s+([\d.,+-]+)\s+([\d.,+-]+)\s+(\w+)\s+([\d.,]+)',
        # Pattern 2: No Dimension Upper Lower MeasuredValue
        r'(\d+)\s+([\d.,+-]+)\s+([\d.,+-]+)\s+([\d.,+-]+)\s+([\d.,]+)',
        # Pattern 3: No Sym Value MeasuredValue (for BURR case)
        r'(\d+)\s+(BURR)\s+([\d.,]+)\s+([\d.,]+)',
        # Pattern 4: Simple No Value Value (minimal case)
        r'(\d+)\s+([\d.,]+)\s+([\d.,]+)$'
    ]
    
    lines = page_text.split('\n')
    for line in lines:
        line = line.strip()
        if not line or not line[0].isdigit():
            continue
            
        # Try each pattern
        for pattern_idx, pattern in enumerate(patterns):
            match = re.search(pattern, line)
            if match:
                groups = match.groups()
                
                # Initialize measurement
                measurement = {
                    "no": groups[0],
                    "sym": "",
                    "dimension": "",
                    "upper": "",
                    "lower": "",
                    "pos": "",
                    "measured_by_vendor": ""
                }
                
                # Pattern-specific parsing
                if pattern_idx == 0:  # Full pattern with symbol and position
                    measurement.update({
                        "sym": groups[1],
                        "dimension": groups[2],
                        "upper": groups[3],
                        "lower": groups[4],
                        "pos": groups[5],
                        "measured_by_vendor": groups[6]
                    })
                elif pattern_idx == 1:  # No symbol, no explicit position
                    measurement.update({
                        "dimension": groups[1],
                        "upper": groups[2],
                        "lower": groups[3],
                        "measured_by_vendor": groups[4]
                    })
                elif pattern_idx == 2:  # BURR case
                    measurement.update({
                        "sym": groups[1],
                        "dimension": groups[2],
                        "measured_by_vendor": groups[3]
                    })
                elif pattern_idx == 3:  # Minimal case
                    measurement.update({
                        "dimension": groups[1],
                        "measured_by_vendor": groups[2]
                    })
                
                measurements.append(measurement)
                break  # Found a match, move to next line
    
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
    
    # Extract measurements from all pages
    all_measurements = []
    for page_num in range(len(doc)):
        page = doc[page_num]
        measurements_on_page = extract_measurement_data_with_coords(page)
        if measurements_on_page:
            for measurement in measurements_on_page:
                measurement['page'] = page_num
            all_measurements.extend(measurements_on_page)

    # Remove duplicates based on measurement number
    unique_measurements = {}
    for measurement in all_measurements:
        key = measurement['no']
        if key not in unique_measurements:
            unique_measurements[key] = measurement
    
    # Convert back to list and sort
    final_measurements = list(unique_measurements.values())
    final_measurements.sort(key=lambda x: int(x['no']) if x['no'].isdigit() else 0)
    
    doc.close()
    
    return {
        "cavity_id": cavity_id,
        "header_info": header_data,
        "measurements": final_measurements
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
app = app  # Export the Flask app directly

if __name__ == '__main__':
    app.run(debug=True)
