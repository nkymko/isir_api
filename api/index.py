def extract_measurement_data_with_coords(page):
    """Extract measurement data from PDF page - Fixed version"""
    measurements = []
    
    # Get all text with coordinates
    text_dict = page.get_text("dict")
    
    # Also try getting words for backup
    words = page.get_text("words")
    
    # Method 1: Try to find measurement table using text blocks
    blocks = page.get_text("blocks")
    
    measurement_found = False
    
    for block in blocks:
        if isinstance(block, tuple) and len(block) >= 5:
            block_text = block[4]  # Text content
            
            # Look for measurement table patterns
            lines = block_text.strip().split('\n')
            
            for i, line in enumerate(lines):
                line = line.strip()
                
                # Skip empty lines and headers
                if not line or 'No.' in line or 'Sym.' in line or 'Dimension' in line:
                    continue
                
                # Try to parse measurement lines
                # Expected format: No Sym Dimension Upper Lower Pos MeasuredByVendor
                parts = line.split()
                
                if len(parts) >= 6:  # Minimum required fields
                    try:
                        # Check if first part is a number (measurement number)
                        if parts[0].isdigit():
                            no = parts[0]
                            
                            # Determine if there's a symbol
                            sym = ""
                            start_idx = 1
                            
                            # Check if second part looks like a symbol (Ø, BURR, etc.)
                            if not _is_numeric_value(parts[1]):
                                sym = parts[1]
                                start_idx = 2
                            
                            # Extract remaining fields
                            if len(parts) >= start_idx + 5:
                                dimension = parts[start_idx]
                                upper = parts[start_idx + 1]
                                lower = parts[start_idx + 2]
                                pos = parts[start_idx + 3]
                                measured_by_vendor = parts[start_idx + 4]
                                
                                measurements.append({
                                    "no": no,
                                    "sym": sym,
                                    "dimension": dimension,
                                    "upper": upper,
                                    "lower": lower,
                                    "pos": pos,
                                    "measured_by_vendor": measured_by_vendor
                                })
                                measurement_found = True
                                
                    except (IndexError, ValueError):
                        continue
    
    # Method 2: If no measurements found, try parsing from raw text
    if not measurement_found:
        page_text = page.get_text("text")
        measurements = _parse_measurements_from_text(page_text)
    
    # Method 3: If still no measurements, try word-by-word analysis
    if not measurements:
        measurements = _extract_from_words(words)
    
    return measurements

def _is_numeric_value(text):
    """Check if text represents a numeric value (including decimals, negatives, etc.)"""
    try:
        float(text.replace('+', '').replace('-', '').replace(',', '.'))
        return True
    except ValueError:
        return False

def _parse_measurements_from_text(text):
    """Parse measurements from raw text using regex patterns"""
    measurements = []
    
    # Split text into lines
    lines = text.split('\n')
    
    # Look for measurement patterns
    measurement_pattern = r'(\d+)\s+([ØøBURR\w]*)\s*([\d.,+-]+)\s*([\d.,+-]+)\s*([\d.,+-]+)\s*(\w+)\s*([\d.,]+)'
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
            
        # Try different parsing approaches
        match = re.search(measurement_pattern, line)
        if match:
            groups = match.groups()
            measurements.append({
                "no": groups[0],
                "sym": groups[1] if groups[1] else "",
                "dimension": groups[2],
                "upper": groups[3],
                "lower": groups[4],
                "pos": groups[5],
                "measured_by_vendor": groups[6] if len(groups) > 6 else ""
            })
    
    return measurements

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
        if any(header in line_text.upper() for header in ['NO.', 'SYM.', 'DIMENSION', 'UPPER', 'LOWER', 'POS.']):
            continue
        
        # Try to parse measurement from this line
        if line_words and line_words[0].isdigit():
            try:
                measurement = _parse_measurement_line(line_words)
                if measurement:
                    measurements.append(measurement)
            except:
                continue
    
    return measurements

def _parse_measurement_line(words):
    """Parse a single measurement line from word list"""
    if len(words) < 6:
        return None
    
    try:
        no = words[0]
        if not no.isdigit():
            return None
        
        # Check for symbol
        sym = ""
        idx = 1
        if not _is_numeric_value(words[1]):
            sym = words[1]
            idx = 2
        
        if len(words) < idx + 5:
            return None
        
        dimension = words[idx]
        upper = words[idx + 1]
        lower = words[idx + 2]
        pos = words[idx + 3]
        measured_by_vendor = words[idx + 4]
        
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

# Also update the main processing function to handle the case where measurements are on page 0
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
    
    # Extract measurements from ALL pages (including first page)
    all_measurements = []
    for page_num in range(len(doc)):  # Changed from range(1, len(doc))
        page = doc[page_num]
        measurements_on_page = extract_measurement_data_with_coords(page)
        if measurements_on_page:
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
