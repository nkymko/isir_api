from flask import Flask, request, jsonify
from flask_cors import CORS
import fitz  # PyMuPDF
import re
from collections import defaultdict
import os
from werkzeug.utils import secure_filename
from datetime import datetime

app = Flask(__name__)
CORS(app)

app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024  # 10MB limit for Vercel

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
    """Check if text represents a numeric value"""
    if not text:
        return False
    try:
        clean_text = text.replace('+', '').replace(',', '.').strip()
        if clean_text.startswith('-'):
            clean_text = clean_text[1:]
        float(clean_text)
        return True
    except ValueError:
        return False


# ------------------ NEW MEASUREMENTS EXTRACTION ------------------ #

def extract_measurement_data_with_coords(page, page_num):
    """Extract measurement data from PDF page and merge vendor measurements."""
    words = page.get_text("words")
    if not words:
        return []

    # Split into left & right halves
    page_width = page.rect.width
    mid_x = page_width / 2
    left_words = [w for w in words if w[0] < mid_x]
    right_words = [w for w in words if w[0] >= mid_x]

    # Parse left as main measurements
    left_measurements = _extract_main_measurements(left_words, page_num)

    # Parse right as vendor measurements
    vendor_values = _extract_vendor_measurements(right_words)

    # Merge vendor value into left_measurements by 'no'
    for m in left_measurements:
        no = m["no"]
        m["measured_by_vendor"] = vendor_values.get(no, None)

    return left_measurements


def _extract_main_measurements(words, page_num):
    """Parse the left table: no, sym, dim, upper, lower, pos."""
    measurements = []
    lines = defaultdict(list)

    for w in words:
        y = round(w[1], -1)
        lines[y].append(w)

    for y in sorted(lines.keys()):
        line_words = sorted(lines[y], key=lambda w: w[0])
        text_parts = [lw[4] for lw in line_words]

        # Skip headers
        if any(h in " ".join(text_parts).upper()
               for h in ["NO.", "SYM.", "DIMENSION", "UPPER", "LOWER", "POS."]):
            continue

        if text_parts and text_parts[0].isdigit():
            m = _parse_main_parts(text_parts)
            if m:
                m["page"] = page_num
                measurements.append(m)

    return measurements


def _parse_main_parts(parts):
    if len(parts) < 4:
        return None

    no = parts[0]
    if not no.isdigit():
        return None

    sym = ""
    idx = 1
    symbol_indicators = ['Ø', 'ø', 'BURR', '⌀', 'φ']
    if idx < len(parts) and any(symbol in parts[idx] for symbol in symbol_indicators):
        sym = parts[idx]
        idx += 1
    elif idx < len(parts) and not _is_numeric_value(parts[idx]):
        sym = parts[idx]
        idx += 1

    dimension = parts[idx] if idx < len(parts) else ""
    upper = parts[idx + 1] if idx + 1 < len(parts) else ""
    lower = parts[idx + 2] if idx + 2 < len(parts) else ""
    pos = parts[idx + 3] if idx + 3 < len(parts) else ""

    return {
        "no": no,
        "sym": sym,
        "dimension": dimension,
        "upper": upper,
        "lower": lower,
        "pos": pos
    }


def _extract_vendor_measurements(words):
    """Extract mapping of no -> measured_by_vendor from right table."""
    vendor_map = {}
    lines = defaultdict(list)

    for w in words:
        y = round(w[1], -1)
        lines[y].append(w)

    for y in sorted(lines.keys()):
        line_words = sorted(lines[y], key=lambda w: w[0])
        text_parts = [lw[4] for lw in line_words]

        if text_parts and text_parts[0].isdigit():
            no = text_parts[0]
            vendor_value = None
            for p in text_parts[1:]:
                if _is_numeric_value(p):
                    vendor_value = p
                    break
            vendor_map[no] = vendor_value

    return vendor_map


# ------------------ PROCESS PDF ------------------ #

def extract_cavity_number_from_filename(filename):
    match = re.search(r'CAV-(\d+)', filename, re.IGNORECASE)
    if match:
        return f"CAV-{match.group(1)}"
    else:
        return os.path.splitext(os.path.basename(filename))[0]


def process_pdf_data(pdf_data, filename):
    try:
        doc = fitz.open(stream=pdf_data, filetype="pdf")
    except Exception as e:
        raise Exception(f"Error opening PDF: {str(e)}")

    cavity_id = extract_cavity_number_from_filename(filename)

    first_page_text = doc[0].get_text("text")
    header_data = extract_header_data(first_page_text)

    all_measurements = []
    for page_num in range(len(doc)):
        page = doc[page_num]
        measurements_on_page = extract_measurement_data_with_coords(page, page_num + 1)
        all_measurements.extend(measurements_on_page)

    # Deduplicate by no + dimension
    unique_measurements = []
    seen = set()
    for m in all_measurements:
        key = (m["no"], m["dimension"])
        if key not in seen:
            seen.add(key)
            unique_measurements.append(m)

    doc.close()

    return {
        "cavity_id": cavity_id,
        "header_info": header_data,
        "measurements": unique_measurements
    }


@app.route('/api/process-pdf', methods=['POST'])
def process_pdf():
    try:
        if 'files' not in request.files:
            return jsonify({"error": "No files provided"}), 400

        files = request.files.getlist('files')
        if not files or all(f.filename == '' for f in files):
            return jsonify({"error": "No files selected"}), 400

        all_data = {}
        errors = []
        success_count = 0

        for file in files:
            filename = secure_filename(file.filename)
            if not allowed_file(filename):
                errors.append(f"{filename}: Invalid file type")
                continue

            pdf_data = file.read()
            if len(pdf_data) > 10 * 1024 * 1024:
                errors.append(f"{filename}: File too large (>10MB)")
                continue

            try:
                result = process_pdf_data(pdf_data, filename)
                cavity_id = result["cavity_id"]
                all_data[cavity_id] = {
                    "filename": filename,
                    "header_info": result["header_info"],
                    "measurements": result["measurements"],
                    "processed_at": datetime.now().isoformat()
                }
                success_count += 1
            except Exception as e:
                errors.append(f"{filename}: {str(e)}")

        if success_count == 0:
            return jsonify({"error": "No files processed", "errors": errors}), 400

        total_measurements = sum(len(v["measurements"]) for v in all_data.values())
        response_data = {
            "success": True,
            "summary": {
                "files_processed": success_count,
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
        return jsonify({"error": "Internal server error", "message": str(e)}), 500


if __name__ == '__main__':
    app.run(debug=True)
