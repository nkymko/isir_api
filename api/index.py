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
    """Extract measurement data from PDF page using coordinate-based method"""
    measurements = []
    words = page.get_text("words")
    lines = defaultdict(list)

    # Group words by y-coordinate (line)
    for w in words:
        lines[round(w[1])].append(w)

    # Pattern to identify measurement-related characters
    measurement_char_pattern = re.compile(r"^(?:\d{1,3}(?:\.\d+)?|[A-Za-z]{1,3}|[©شPपp04⌀∅⊕⊙○●◯◉⊗⊘∀∁∂∃∄∅∆∇∈∉∊∋∌∍∎∏∐∑−∓∔∕∖∗∘∙√∛∜∝∞∟∠∡∢∣∤∥∦∧∨∩∪∫∬∭∮∯∰∱∲∳∴∵∶∷∸∹∺∻∼∽∾∿≀≁≂≃≄≅≆≇≈≉≊≋≌≍≎≏≐≑≒≓≔≕≖≗≘≙≚≛≜≝≞≟≠≡≢≣≤≥≦≧≨≩≪≫≬≭≮≯≰≱≲≳≴≵≶≷≸≹≺≻≼≽≾≿⊀⊁⊂⊃⊄⊅⊆⊇⊈⊉⊊⊋⊌⊍⊎⊏⊐⊑⊒⊓⊔⊕⊖⊗⊘⊙⊚⊛⊜⊝⊞⊟⊠⊡⊢⊣⊤⊥⊦⊧⊨⊩⊪⊫⊬⊭⊮⊯⊰⊱⊲⊳⊴⊵⊶⊷⊸⊹⊺⊻⊼⊽⊾⊿⋀⋁⋂⋃⋄⋅⋆⋇⋈⋉⋊⋋⋌⋍⋎⋏⋐⋑⋒⋓⋔⋕⋖⋗⋘⋙⋚⋛⋜⋝⋞⋟⋠⋡⋢⋣⋤⋥⋦⋧⋨⋩⋪⋫⋬⋭⋮⋯⋰⋱⋲⋳⋴⋵⋶⋷⋸⋹⋺⋻⋼⋽⋾⋿φψχωΩαβγδεζηθικλμνξοπρστυφχψωΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩ℀℁ℂ℃℄℅℆ℇ℈℉ℊℋℌℍℎℏℐℑℒℓ℔ℕ№℗℘ℙℚℛℜℝ℞℟℠℡™℣ℤ℥Ω℧ℨ℩KÅℬℭ℮ℯℰℱℲℳℴℵℶℷℸℹ℺℻ℼℽℾℿ⅀⅁⅂⅃⅄ⅅⅆⅇⅈⅉ⅊⅋⅌⅍ⅎ⅏►◄▲▼◆◇○●◯◉⊗⊘△▲▽▼◁▷⧺⧻⦿⊙⊚⊛⊜⊝⊕⊖⊗⊘⊙⊚⊛⊜⊝⊞⊟⊠⊡]|[ĀĂĄĆĈĊČĎĐĒĔĖĘĚĜĞĠĢĤĦĨĪĬĮİĲĴĶĹĻĽĿŁŃŅŇŊŌŎŐŒŔŖŘŚŜŞŠŢŤŦŨŪŬŮŰŲŴŶŸŹŻŽ])$", re.UNICODE)

    for y in sorted(lines.keys()):
        line_words = sorted(lines[y], key=lambda w: w[0])
        text_line = [w[4] for w in line_words if measurement_char_pattern.match(w[4])]

        entries_to_process = []
        
        # Handle lines with multiple measurements
        if 12 <= len(text_line) <= 14:
            split_point = len(text_line) // 2
            entries_to_process.append(text_line[:split_point])
            entries_to_process.append(text_line[split_point:])
        elif 6 <= len(text_line) <= 7:
            entries_to_process.append(text_line)

        if entries_to_process:
            try:
                for entry_words in entries_to_process:
                    if len(entry_words) < 2:
                        continue
                        
                    no = entry_words.pop(0)
                    sym = ""
                    
                    # Check if next item is a symbol
                    if entry_words and not re.match(r'^\d+\.?\d*$', entry_words[0]):
                        sym = entry_words.pop(0)
                    
                    # Handle cases where we might have more fields
                    if len(entry_words) >= 5:
                        dimension = entry_words[0] if len(entry_words) > 0 else ""
                        upper = entry_words[1] if len(entry_words) > 1 else ""
                        lower = entry_words[2] if len(entry_words) > 2 else ""
                        pos = entry_words[3] if len(entry_words) > 3 else ""
                        measured_by_vendor = entry_words[4] if len(entry_words) > 4 else ""
                        
                        # Check for additional fields (DENSO measurement and assessment)
                        measured_by_denso = entry_words[5] if len(entry_words) > 5 else ""
                        assessment = entry_words[6] if len(entry_words) > 6 else ""

                        measurements.append({
                            "no": no,
                            "sym": sym,
                            "dimension": dimension,
                            "upper": upper,
                            "lower": lower,
                            "pos": pos,
                            "measured_by_vendor": measured_by_vendor,
                            "measured_by_denso": measured_by_denso,
                            "assessment": assessment
                        })
            except (IndexError, ValueError):
                continue
                
    return measurements

def parse_measurement_row(texts):
    """Parse a single line that may contain 1 or 2 measurement rows"""
    measurements = []
    
    # Find positions of row numbers (1-3 digit numbers)
    row_positions = []
    for i, text in enumerate(texts):
        if re.match(r'^\d{1,3}$', text) and 1 <= int(text) <= 200:
            row_positions.append(i)
    
    if not row_positions:
        return measurements
    
    # Process each row found
    for i, pos in enumerate(row_positions):
        # Determine end position for this row's data
        end_pos = row_positions[i + 1] if i + 1 < len(row_positions) else len(texts)
        row_texts = texts[pos:end_pos]
        
        measurement = parse_single_measurement(row_texts)
        if measurement:
            measurements.append(measurement)
    
    return measurements

def parse_single_measurement(row_texts):
    """Parse a single measurement row"""
    if len(row_texts) < 2:
        return None
    
    # Initialize measurement data
    measurement = {
        'no': '',
        'sym': '',
        'dimension': '',
        'upper': '',
        'lower': '',
        'pos': '',
        'measured_by_vendor': '',
        'measured_by_denso': '',
        'assessment': ''
    }
    
    # Row number is first
    measurement['no'] = row_texts[0]
    
    # Process remaining texts
    remaining = row_texts[1:]
    idx = 0
    
    # Helper functions for text classification
    def is_symbol(text):
        """Check if text is a symbol"""
        symbol_patterns = [r'^[ØøΦφ◯○⊙⊕⊖⊗⊘∅]', r'^[A-Za-z]{1,4}$', r'^BURR$']
        return any(re.match(pattern, text) for pattern in symbol_patterns) and not is_number(text)
    
    def is_number(text):
        """Check if text is a pure number"""
        return re.match(r'^\d+(\.\d+)?$', text) is not None
    
    def is_tolerance(text):
        """Check if text is a tolerance value"""
        return re.match(r'^[+\-]?\d*\.?\d+$', text) is not None or re.match(r'^[+\-]0\.\d+$', text) is not None
    
    def is_measurement_value(text):
        """Check if text is a measurement value"""
        try:
            val = float(text)
            return 0 < val < 1000  # Reasonable measurement range
        except:
            return False
    
    # Parse fields in order
    # 1. Symbol (optional)
    if idx < len(remaining) and is_symbol(remaining[idx]) and not is_number(remaining[idx]):
        measurement['sym'] = remaining[idx]
        idx += 1
    
    # 2. Dimension
    if idx < len(remaining) and is_number(remaining[idx]):
        measurement['dimension'] = remaining[idx]
        idx += 1
    
    # 3. Upper tolerance
    if idx < len(remaining) and is_tolerance(remaining[idx]):
        measurement['upper'] = remaining[idx]
        idx += 1
    
    # 4. Lower tolerance
    if idx < len(remaining) and is_tolerance(remaining[idx]):
        measurement['lower'] = remaining[idx]
        idx += 1
    
    # 5. Position (text that's not a number)
    if idx < len(remaining) and not is_number(remaining[idx]) and not is_measurement_value(remaining[idx]):
        measurement['pos'] = remaining[idx]
        idx += 1
    
    # 6. Measured by Vendor
    if idx < len(remaining) and is_measurement_value(remaining[idx]):
        measurement['measured_by_vendor'] = remaining[idx]
        idx += 1
    
    # 7. Measured by DENSO
    if idx < len(remaining) and is_measurement_value(remaining[idx]):
        measurement['measured_by_denso'] = remaining[idx]
        idx += 1
    
    # 8. Assessment (any remaining text)
    if idx < len(remaining):
        measurement['assessment'] = remaining[idx]
    
    return measurement

def extract_table_data_from_page(page):
    """Extract table data from PDF page using improved text parsing"""
    try:
        # Try the coordinate-based method first
        measurements = extract_measurement_data_with_coords(page)
        
        # If that doesn't work, fall back to simple text extraction
        if not measurements:
            text = page.get_text()
            lines = text.split('\n')
            
            for line in lines:
                # Look for lines that start with numbers (measurement rows)
                if re.match(r'^\d+\s+', line.strip()):
                    texts = line.split()
                    parsed_measurements = parse_measurement_row(texts)
                    measurements.extend(parsed_measurements)
        
        return measurements
        
    except Exception as e:
        print(f"Error extracting table data: {str(e)}")
        return []

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
    
    # Extract measurements from page 2 onwards (index 1)
    all_measurements = []
    for page_num in range(1, len(doc)):
        page = doc[page_num]
        measurements_on_page = extract_table_data_from_page(page)
        if measurements_on_page:
            all_measurements.extend(measurements_on_page)

    # Remove duplicates and sort by row number
    unique_measurements = []
    seen = set()
    
    for measurement in all_measurements:
        # Create a key for duplicate detection
        key = (measurement['no'], measurement['dimension'], measurement['measured_by_vendor'])
        if key not in seen:
            seen.add(key)
            unique_measurements.append(measurement)
    
    # Sort by row number
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

# Export the Flask app for Vercel
if __name__ == '__main__':
    app.run(debug=True)
