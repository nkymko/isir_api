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
    """Extract measurement data from PDF page - improved to handle multiple formats"""
    measurements = []
    words = page.get_text("words")
    lines = defaultdict(list)

    for w in words:
        lines[round(w[1])].append(w)

    # Enhanced pattern to match measurement characters and symbols
    measurement_char_pattern = re.compile(
        r"^(?:\d{1,3}(?:\.\d+)?|[A-Za-z]{1,3}|[©شPपp04⌀∅⊕⊙○●◯◉⊗⊘∀∁∂∃∄∅∆∇∈∉∊∋∌∍∎∏∐∑−∓∔∕∖∗∘∙√∛∜∝∞∟∠∡∢∣∤∥∦∧∨∩∪∫∬∭∮∯∰∱∲∳∴∵∶∷∸∹∺∻∼∽∾∿≀≁≂≃≄≅≆≇≈≉≊≋≌≍≎≏≐≑≒≓≔≕≖≗≘≙≚≛≜≝≞≟≠≡≢≣≤≥≦≧≨≩≪≫≬≭≮≯≰≱≲≳≴≵≶≷≸≹≺≻≼≽≾≿⊀⊁⊂⊃⊄⊅⊆⊇⊈⊉⊊⊋⊌⊍⊎⊏⊐⊑⊒⊓⊔⊕⊖⊗⊘⊙⊚⊛⊜⊝⊞⊟⊠⊡⊢⊣⊤⊥⊦⊧⊨⊩⊪⊫⊬⊭⊮⊯⊰⊱⊲⊳⊴⊵⊶⊷⊸⊹⊺⊻⊼⊽⊾⊿⋀⋁⋂⋃⋄⋅⋆⋇⋈⋉⋊⋋⋌⋍⋎⋏⋐⋑⋒⋓⋔⋕⋖⋗⋘⋙⋚⋛⋜⋝⋞⋟⋠⋡⋢⋣⋤⋥⋦⋧⋨⋩⋪⋫⋬⋭⋮⋯⋰⋱⋲⋳⋴⋵⋶⋷⋸⋹⋺⋻⋼⋽⋾⋿φψχωΩαβγδεζηθικλμνξοπρστυφχψωΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩ℀℁ℂ℃℄℅℆ℇ℈℉ℊℋℌℍℎℏℐℑℒℓ℔ℕ№℗℘ℙℚℛℜℝ℞℟℠℡™℣ℤ℥Ω℧ℨ℩KÅℬℭ℮ℯℰℱℲℳℴℵℶℷℸℹ℺℻ℼℽℾℿ⅀⅁⅂⅃⅄ⅅⅆⅇⅈⅉ⅊⅋⅌⅍ⅎ⅏►◄▲▼◆◇○●◯◉⊗⊘△▲▽▼◁▷⧺⧻⦿⊙⊚⊛⊜⊝⊕⊖⊗⊘⊙⊚⊛⊜⊝⊞⊟⊠⊡Ø]|[ĀĂĄĆĈĊČĎĐĒĔĖĘĚĜĞĠĢĤĦĨĪĬĮİĲĴĶĹĻĽĿŁŃŅŇŊŌŎŐŒŔŖŘŚŜŞŠŢŤŦŨŪŬŮŰŲŴŶŸŹŻŽ]|BURR|\+\d|\-\d)$", 
        re.UNICODE
    )

    # Enhanced number/value pattern to catch decimals, tolerances, and positions
    value_pattern = re.compile(r'^[+-]?\d+(?:[.,]\d+)?(?:[+-]\d+(?:[.,]\d+)?)*$|^Bottom$|^Top$|^Left$|^Right$|^Center$')

    for y in sorted(lines.keys()):
        line_words = sorted(lines[y], key=lambda w: w[0])
        text_line = [w[4] for w in line_words if measurement_char_pattern.match(w[4]) or value_pattern.match(w[4])]

        if len(text_line) < 4:  # Skip lines with too few elements
            continue

        entries_to_process = []
        
        # Handle different line formats
        if 12 <= len(text_line) <= 14:
            # Format like T4E CAV-1: split into two measurements per line
            split_point = len(text_line) // 2
            entries_to_process.append(text_line[:split_point])
            entries_to_process.append(text_line[split_point:])
        elif 6 <= len(text_line) <= 7:
            # Format like T4E CAV-1: single measurement per line
            entries_to_process.append(text_line)
        elif 4 <= len(text_line) <= 6:
            # Format like T1 Teste CAV-4: different structure
            entries_to_process.append(text_line)

        for entry_words in entries_to_process:
            try:
                # Try to extract measurement data
                if len(entry_words) >= 4:
                    no = entry_words[0]
                    
                    # Skip if first element is not a number (measurement ID)
                    if not re.match(r'^\d+$', no):
                        continue
                    
                    remaining = entry_words[1:]
                    sym = ""
                    
                    # Check if second element is a symbol
                    if remaining and not value_pattern.match(remaining[0]) and not re.match(r'^\d+(?:[.,]\d+)?$', remaining[0]):
                        sym = remaining[0]
                        remaining = remaining[1:]
                    
                    # Need at least 3 more elements for dimension, upper, lower
                    if len(remaining) >= 3:
                        dimension = remaining[0] if remaining[0] else ""
                        upper = remaining[1] if len(remaining) > 1 else ""
                        lower = remaining[2] if len(remaining) > 2 else ""
                        
                        # Position and measured_by_vendor (optional)
                        pos = remaining[3] if len(remaining) > 3 else ""
                        measured_by_vendor = remaining[4] if len(remaining) > 4 else ""

                        measurements.append({
                            "no": no,
                            "sym": sym,
                            "dimension": dimension,
                            "upper": upper,
                            "lower": lower,
                            "pos": pos,
                            "measured_by_vendor": measured_by_vendor
                        })
            except (IndexError, ValueError) as e:
                continue

    # Alternative approach for formats that don't match the above patterns
    if not measurements:
        measurements = extract_alternative_format(page)
                
    return measurements

def extract_alternative_format(page):
    """Alternative extraction method for different PDF formats"""
    measurements = []
    text = page.get_text()
    
    # Look for measurement patterns in the text
    # Pattern for lines like: "1 Ø 10 +0.012 -0.5 Bottom"
    pattern = re.compile(r'(\d+)\s+([ØøOo]?[^0-9\s]*?)\s*([\d.,+-]+)\s*([\d.,+-]+)\s*([\d.,+-]+)\s*(\w*)', re.MULTILINE)
    
    matches = pattern.findall(text)
    for match in matches:
        no, sym, dim, upper, lower, pos = match
        if no and dim:  # Ensure we have at least number and dimension
            measurements.append({
                "no": no.strip(),
                "sym": sym.strip(),
                "dimension": dim.strip(),
                "upper": upper.strip(),
                "lower": lower.strip(),
                "pos": pos.strip(),
                "measured_by_vendor": ""
            })
    
    # If still no measurements found, try a simpler approach
    if not measurements:
        lines = text.split('\n')
        for line in lines:
            # Look for lines starting with numbers
            if re.match(r'^\s*\d+\s+', line):
                parts = line.strip().split()
                if len(parts) >= 4:
                    try:
                        measurements.append({
                            "no": parts[0],
                            "sym": parts[1] if not re.match(r'^[\d.,+-]+$', parts[1]) else "",
                            "dimension": parts[2] if not re.match(r'^[\d.,+-]+$', parts[1]) else parts[1],
                            "upper": parts[3] if not re.match(r'^[\d.,+-]+$', parts[1]) else parts[2],
                            "lower": parts[4] if len(parts) > 4 and not re.match(r'^[\d.,+-]+$', parts[1]) else parts[3],
                            "pos": parts[5] if len(parts) > 5 else "",
                            "measured_by_vendor": ""
                        })
                    except IndexError:
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
    
    # Extract measurements from all pages (including first page for some formats)
    all_measurements = []
    for page_num in range(len(doc)):
        page = doc[page_num]
        measurements_on_page = extract_measurement_data_with_coords(page)
        if measurements_on_page:
            all_measurements.extend(measurements_on_page)

    # Remove duplicates and sort
    unique_measurements = []
    seen = set()
    for measurement in all_measurements:
        # Create a tuple of key values to check for duplicates
        key = (measurement['no'], measurement['sym'], measurement['dimension'])
        if key not in seen:
            seen.add(key)
            unique_measurements.append(measurement)
    
    # Sort by number
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
