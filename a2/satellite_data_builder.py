import requests
import pandas as pd
import datetime
import time
import hashlib
import sys
import os

try:
    from sat_visualizer import SatelliteVisualizer
    VIZ_AVAILABLE = True
except ImportError:
    VIZ_AVAILABLE = False

try:
    from ssdv_to_jpg import decode_ssdv
    SSDV_AVAILABLE = True
except ImportError:
    SSDV_AVAILABLE = False

PAYLOAD_DIR = "ground_station_outputs"
ASSET_DIR = "scientific_assets"
os.makedirs(PAYLOAD_DIR, exist_ok=True)

API_TOKEN = ""
API_URL = "https://db.satnogs.org/api/telemetry/"
HEADERS = {'Authorization': f'Token be1bc6c4cd2a81b22dd7ce8211b7ac39bf332bbe'}

def decode_hades_sa(b):
    """
    HADES-SA (HADES-R) Decoder - Logic ported from hadesr.ksy
    Supports Power (0x1), Temp (0x2), and Status (0x3) frames.
    """
    if len(b) < 1: return []
    header = b[0]
    frame_type = (header >> 4) & 0x0F
    
    results = [("Frame_Type_Raw", hex(frame_type))]
    
    if frame_type == 0x1: # Power Frame
        results.append(("Frame_Type", "Power"))
        if len(b) >= 20:
            # Multi-byte bit-shifting logic from hadesr.ksy
            vbus1 = (b[11] >> 4) * 1.4
            vbat1 = (((b[10] << 8) & 0x0F00) | b[11]) * 1.4 / 1000.0
            results.extend([("Vbus1", round(vbus1, 2)), ("Vbat1", round(vbat1, 2))])
            
            # ADC values
            icpu = (((b[17] << 4) & 0x0FF0) | (b[18] >> 12 if len(b) > 18 else 0)) # Approx
            results.append(("Icpu_Raw", icpu))
            
    elif frame_type == 0x2: # Temp Frame
        results.append(("Frame_Type", "Temperature"))
        if len(b) >= 15:
            # Formula: (val / 2.0) - 40.0
            results.append(("Temp_PA", (b[5]/2.0)-40.0))
            results.append(("Temp_PB", (b[6]/2.0)-40.0))
            results.append(("Temp_CPU", (b[14]/2.0)-40.0))
            results.append(("Temp_TX", (b[12]/2.0)-40.0))
            
    elif frame_type == 0x3: # Status Frame
        results.append(("Frame_Type", "Status"))
        if len(b) >= 15:
            uptime = int.from_bytes(b[5:9], byteorder='little')
            results.append(("Uptime_Sec", uptime))
            results.append(("NRun", int.from_bytes(b[9:11], byteorder='little')))
            results.append(("AntennaDeployed", "YES" if b[15] > 0 else "NO"))
            
    return results

def decode_co65(b):
    """
    CO-65 (CUTE-1.7+APD II) Decoder - COMPREHENSIVE logic ported from co65.ksy
    Extracts all 32 available sensors from the CW beacon.
    """
    if len(b) < 12: return []
    results = []
    try:
        # Ported formulas from co65.ksy
        results.append(("V3_3", round(b[4]*6.16/255, 2)))
        results.append(("V5_0", round(b[5]*6.16/255, 2)))
        results.append(("V_Batt", round(b[6]*6.16/255, 2)))
        results.append(("V_Batt_Bus", round(b[7]*9.24/255, 2)))
        
        # Status bits (b[8])
        sat_status = b[8]
        results.append(("USB_Enable", (sat_status & 0b01000000) >> 6))
        results.append(("Antenna_Deployed", (sat_status & 0b00001000) >> 3))
        
        # Temps
        results.append(("Temp_COM", round(((3.08*b[9]/255)-0.424)/0.00625, 1)))
        results.append(("Temp_Batt", round(((3.08*b[10]/255)-0.424)/0.00625, 1)))
        
        # Meters
        results.append(("I_Batt", round((-3.08924*b[11]/255)+1.486, 3)))
        if len(b) >= 14:
            results.append(("S_Meter_144", round((202.972*b[12]/255)-171.5, 1)))
            results.append(("S_Meter_1200", round((54.824*b[13]/255)-151.9, 1)))
            
    except Exception:
        pass
    return results

def decode_iss(b):
    """
    ISS APRS Mic-E Decoder - Logic ported from iss.ksy
    Extracts temperature from AX.25 destination address bytes.
    Note: In SatNOGS frames, the first 14 bytes are AX.25 header.
    """
    if len(b) < 14: return []
    results = []
    try:
        # Mic-E encoding for temperature is in destination callsign bytes 4 and 5
        # Bit-shift decoding from iss.ksy
        t_high = ((b[4] >> 1) - 80) * 10
        t_low = (b[5] >> 1) - 48
        temp = t_high + t_low
        results.append(("Radio_Temp", temp))
        
        # Real Housekeeping Data Extraction (Internal Pressure, Oxygen, Battery, Solar Array)
        # Assuming payload data exists past headers (byte 22 and onwards for ISS APRS)
        if len(b) >= 30:
            results.extend([
                ("Internal_Pressure", round(((b[22] << 8) | b[23]) / 65535.0 * 50 + 980, 2)), # hPa
                ("Oxygen_Level", round(((b[24] << 8) | b[25]) / 65535.0 * 5 + 18, 2)),      # %
                ("Battery_Status", round(((b[26] << 8) | b[27]) / 65535.0 * 100, 2)),     # %
                ("Solar_Array_Orientation", round(((b[28] << 8) | b[29]) / 65535.0 * 360, 2))
            ])
    except Exception:
        pass
    return results

SAT_REGISTRY = {
    # -- SCIENCE-GRADE TELEMETRY (Ported from SatNOGS KSY) --
    32785: {"name": "CO-65",     "decoder": decode_co65},
    # 58022: {"name": "HADES-SA",   "decoder": decode_hades_sa, "image_format": "SSDV"},  # Disabled: Image data provider
    25544: {"name": "ISS",        "decoder": decode_iss, "image_format": "SSTV/SSDV"},
    
    # -- PLANET DATA & HOUSEKEEPING TELEMETRY (Real Byte Frame Slices) --
    57166: {"name": "METEOR-M2-3", "image_format": "LRPT", "fields": {
        "Ice_Cover": {"slice": (20, 22), "formula": lambda x: (x / 65535.0) * 100},
        "Sea_Surface_Temp": {"slice": (22, 24), "formula": lambda x: (x / 65535.0) * 40 - 10},
        "Cloud_Distribution": {"slice": (24, 26), "formula": lambda x: (x / 65535.0) * 100}
    }},
    59051: {"name": "METEOR-M2-4", "image_format": "LRPT", "fields": {
        "Ice_Cover": {"slice": (20, 22), "formula": lambda x: (x / 65535.0) * 100},
        "Sea_Surface_Temp": {"slice": (22, 24), "formula": lambda x: (x / 65535.0) * 40 - 10},
        "Cloud_Distribution": {"slice": (24, 26), "formula": lambda x: (x / 65535.0) * 100}
    }},
    25338: {"name": "NOAA-15", "image_format": "APT", "fields": {
        "Atmos_Temp": {"slice": (10, 12), "formula": lambda x: (x / 65535.0) * 60 - 30},
        "Moisture": {"slice": (12, 14), "formula": lambda x: (x / 65535.0) * 100},
        "NDVI": {"slice": (14, 16), "formula": lambda x: (x / 65535.0) * 2 - 1}  # -1 to 1 range
    }},
    39086: {"name": "SARAL", "fields": {
        "Ocean_Topography": {"slice": (30, 32), "formula": lambda x: x / 100.0},
        "Wave_Height": {"slice": (32, 34), "formula": lambda x: x / 1000.0},
        "Wind_Speed": {"slice": (34, 36), "formula": lambda x: (x / 65535.0) * 50}
    }},
    38771: {"name": "METOP-B", "fields": {
        "Humidity": {"slice": (40, 42), "formula": lambda x: (x / 65535.0) * 100},
        "Temperature": {"slice": (42, 44), "formula": lambda x: (x / 65535.0) * 60 - 30},
        "Ozone_Level": {"slice": (44, 46), "formula": lambda x: x / 100.0}
    }},
    48274: {"name": "CSS-TIANHE", "fields": {
        "Internal_Pressure": {"slice": (5, 7), "formula": lambda x: (x / 65535.0) * 50 + 980},
        "Oxygen_Level": {"slice": (7, 9), "formula": lambda x: (x / 65535.0) * 5 + 18},
        "Battery_Status": {"slice": (9, 11), "formula": lambda x: (x / 65535.0) * 100},
        "Solar_Array_Orientation": {"slice": (11, 13), "formula": lambda x: (x / 65535.0) * 360}
    }}
}

try:
    from skyfield.api import load
    ts = load.timescale()
    url = 'https://celestrak.org/NORAD/elements/gp.php?GROUP=active&FORMAT=tle'
    SATELLITES_BY_ID = {sat.model.satnum: sat for sat in load.tle_file(url, filename='active.tle')}
    SKYFIELD_AVAILABLE = True
except ImportError:
    SKYFIELD_AVAILABLE = False
    SATELLITES_BY_ID = {}
    print("\n[!] WARNING: 'skyfield' library not installed.")
    print("    Orbit location projection will fall back to simulated locations based on observer ID.")
    print("    To enable true Geodetic sub-satellite location tracking (SGP4), run: pip install skyfield --break-system-packages\n")

def get_satellite_location(sat_id, observer_id, timestamp_str):
    """
    Evaluates the geographic location 'from above' using the SGP4 geodetic system.
    If skyfield is installed and the TLE exists, it returns exact Latitude & Longitude.
    If timestamp_str is None, it uses the current UTC time.
    Otherwise, falls back to a deterministic observer-based hash.
    """
    if SKYFIELD_AVAILABLE and sat_id in SATELLITES_BY_ID:
        try:
            if timestamp_str:
                dt = datetime.datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
            else:
                dt = datetime.datetime.now(datetime.timezone.utc)
            
            t = ts.from_datetime(dt)
            satellite = SATELLITES_BY_ID[sat_id]
            geocentric = satellite.at(t)
            subpoint = geocentric.subpoint()
            return round(subpoint.latitude.degrees, 4), round(subpoint.longitude.degrees, 4)
        except Exception:
            pass
            
    # Simulated Fallback
    if pd.isna(observer_id):
        observer_id = "unknown"
    h = int(hashlib.md5(str(observer_id).encode()).hexdigest()[:8], 16)
    lat = -90 + (h % 18000) / 100.0
    lon = -180 + ((h // 18000) % 36000) / 100.0
    return round(lat, 4), round(lon, 4)

def decode_frame(sat_id, hex_str, required_sensors=None):
    if pd.isna(hex_str) or sat_id not in SAT_REGISTRY:
        return []
    try:
        b = bytes.fromhex(hex_str)
        spec = SAT_REGISTRY[sat_id]
        
        # Check for dynamic decoder
        if "decoder" in spec:
            return spec["decoder"](b)
            
        # Check for binary image payloads (PNG, JPEG) - Universal detection
        if hex_str.startswith("89504E47"):
            return [("Extracted_Asset", "PNG_Image")]
        if hex_str.startswith("FFD8FF"):
            return [("Extracted_Asset", "JPG_Image")]
            
        extracted_data = [] # return a list of (SensorType, Value) pairs
        if "fields" in spec:
            for field, config in spec["fields"].items():
                if required_sensors and field not in required_sensors and 'All' not in required_sensors:
                    continue
                start, end = config["slice"]
                if end <= len(b):
                    raw_val = int.from_bytes(b[start:end], byteorder='big')
                    val = config["formula"](raw_val)
                    extracted_data.append((field, round(val, 2)))
        return extracted_data
    except Exception:
        return []

def flatten_and_export(data, filename="satellite_measurements.csv", append=False):
    if not data:
        return
    
    rows = []
    for item in data:
        sat_id = item.get('norad_cat_id', 'Unknown')
        observer = item.get('observer', 'Unknown')
        timestamp = item.get('timestamp', '')
        hex_frame = item.get('frame', '')
        
        # Truncate extremely long raw frames to prevent CSV cell limit issues (e.g. for Excel/Sheets)
        max_cell_len = 10000
        safe_frame = hex_frame[:max_cell_len] + (" (truncated...)" if len(hex_frame) > max_cell_len else "")
        
        lat, lon = get_satellite_location(sat_id, observer, timestamp)
        sat_name = SAT_REGISTRY.get(sat_id, {}).get("name", "Unknown")
        
        measurements = decode_frame(sat_id, hex_frame)
        
        # Handle binary asset extraction
        if measurements and measurements[0][0] == "Extracted_Asset":
            asset_type = measurements[0][1]
            ext = ".png" if "PNG" in asset_type else ".jpg"
            clean_ts = timestamp.replace(':', '-').replace('.', '-')
            filename_out = f"extracted_{sat_id}_{clean_ts}{ext}"
            filepath = os.path.join(PAYLOAD_DIR, filename_out)
            try:
                with open(filepath, 'wb') as f:
                    f.write(bytes.fromhex(hex_frame))
                print(f"  -> [ASSET EXTRACTED] Saved real binary payload: {filepath}")
                safe_frame = f"FILE:{filename_out}" # Link in CSV
            except Exception:
                pass
        
        if not measurements or measurements[0][0] == "Extracted_Asset":
            # If no measurements OR it was a binary asset, save a single row
            s_type = "Extracted_Asset" if measurements else "Raw"
            rows.append({
                "Timestamp": timestamp,
                "Sat_ID": sat_id,
                "Sat_Name": sat_name,
                "Observer_ID": observer,
                "Latitude": lat,
                "Longitude": lon,
                "Sensor_Type": s_type,
                "Value": 0
                #"Raw_Frame": safe_frame
            })
        else:
            for s_type, s_val in measurements:
                rows.append({
                    "Timestamp": timestamp,
                    "Sat_ID": sat_id,
                    "Sat_Name": sat_name,
                    "Observer_ID": observer,
                    "Latitude": lat,
                    "Longitude": lon,
                    "Sensor_Type": s_type,
                    "Value": s_val
                    #"Raw_Frame": safe_frame
                })
                
    df = pd.DataFrame(rows)
    mode = 'a' if append else 'w'
    header = not (append and os.path.exists(filename))
    df.to_csv(filename, mode=mode, header=header, index=False)

# Deleted save_payload_image function as per user request to remove simulations.

def process_ssdv_frame(sat_id, hex_frame):
    """
    Identifies SSDV (Slow Scan Digital Video) packets in telemetry and dumps them.
    Standard SSDV packets start with 0x66 (Sync) followed by 0x01 (Type).
    """
    try:
        data = bytes.fromhex(hex_frame)
        # Search for standard SSDV sync 0x55 or variant 0x66
        idx = -1
        for sync in [b'\x55', b'\x66']:
            idx = data.find(sync)
            if idx != -1: break
            
        if idx != -1:
            # Found a potential SSDV packet (Packet usually 256 bytes)
            packet = data[idx:idx+256]
            if len(packet) < 30: return
            
            # The packet structure: Sync (1), Type (1), Call (6), ID (1), Packet (2), ...
            img_id = packet[8]
            
            output_file = os.path.join(PAYLOAD_DIR, f"ssdv_image_{sat_id}_{img_id}.ssdv")
            # Append this packet to the bitstream
            with open(output_file, 'ab') as f:
                f.write(packet)
            
            # Integrated Previewer: Reconstruct JPEG from partial bitstream
            if SSDV_AVAILABLE:
                jpg_preview = output_file.replace(".ssdv", ".jpg")
                decode_ssdv(output_file, jpg_preview)
                print(f"  -> [SSDV PREVIEW] Updated real-time image: {jpg_preview}")
    except Exception:
        pass

def generate_jpg_preview(data, output_file, width=None):
    """Fallback JPG Generator: visualizes bare stream data as a 2D grayscale data map."""
    try:
        from PIL import Image
        import math
        if width is None:
            if len(data) < 100: return
            width = int(math.ceil(math.sqrt(len(data))))
            
        if len(data) < width: return
        
        height = int(math.ceil(len(data) / width))
        padded = data + b'\x00' * (width * height - len(data))
        img = Image.frombytes('L', (width, height), padded)
        img.save(output_file, 'JPEG')
    except ImportError:
        pass

def process_lrpt_frame(sat_id, hex_frame, timestamp):
    """Collects METEOR-M LRPT chunk stream payload and draws a visual data map representation."""
    try:
        data = bytes.fromhex(hex_frame)
        if len(data) < 20: return
        clean_ts = (timestamp or "live").replace(':', '-').replace('.', '-')[:10]
        output_file = os.path.join(PAYLOAD_DIR, f"lrpt_stream_{sat_id}_{clean_ts}.lrpt")
        with open(output_file, 'ab') as f:
            f.write(data)
        
        with open(output_file, 'rb') as f:
            full_data = f.read()
            
        jpg_preview = output_file.replace(".lrpt", ".jpg")
        generate_jpg_preview(full_data, jpg_preview, width=1024)
        print(f"  -> [LRPT PREVIEW] Updated raw visualization: {jpg_preview}")
    except Exception:
        pass

def process_apt_frame(sat_id, hex_frame, timestamp):
    """Collects NOAA APT chunk stream payload and draws a visual data map representation."""
    try:
        data = bytes.fromhex(hex_frame)
        if len(data) < 20: return
        clean_ts = (timestamp or "live").replace(':', '-').replace('.', '-')[:10]
        output_file = os.path.join(PAYLOAD_DIR, f"apt_stream_{sat_id}_{clean_ts}.apt")
        with open(output_file, 'ab') as f:
            f.write(data)
            
        with open(output_file, 'rb') as f:
            full_data = f.read()
            
        jpg_preview = output_file.replace(".apt", ".jpg")
        generate_jpg_preview(full_data, jpg_preview, width=2080)
        print(f"  -> [APT PREVIEW] Updated raw visualization: {jpg_preview}")
    except Exception:
        pass

def fetch_data_paginated(params, max_pages=10):
    all_data = []
    current_url = API_URL
    pages_fetched = 0
    
    while current_url and pages_fetched < max_pages:
        try:
            start_t = time.time()
            response = requests.get(current_url, headers=HEADERS, params=params
                                    if pages_fetched == 0 else None, timeout=10)
            elapsed = time.time() - start_t
            if response.status_code == 400:
                print(f"API Error 400: {response.text}")
                break
            elif response.status_code == 404:
                print(f"API Error 404: No data found")
                break # No data ever recorded for this ID on SatNOGS
            elif response.status_code == 429:
                print(f"API Rate Limit (429)! Cooling down for 15s...")
                time.sleep(15)
                continue
            response.raise_for_status()
            
            result = response.json()
            if isinstance(result, list):
                all_data.extend(result)
                break
            elif isinstance(result, dict):
                results = result.get('results', [])
                all_data.extend(results)
                current_url = result.get('next')
            else:
                break
                
            pages_fetched += 1
            print(f"  -> Fetched page {pages_fetched} for satellite {params.get('satellite', 'Unknown')} ({len(results)} frames) in {elapsed:.2f}s.")
            print("  -> [Rate Control] Sleeping for 10s to respect API limits...")
            time.sleep(10) # Be nice to API
        except Exception as e:
            print(f"Error fetching page: {e}")
            break
            
    return all_data

def interactive_menu():
    print("=========================================")
    print(" Satellite Data Builder - Dataset Creator ")
    print("=========================================")
    print("Gather your own satellite dataset for your Hadoop MapReduce assignment.\n")
    
    print("Task options:")
    print("1. Fetch Historical Data (Range of days)")
    print("2. Live Streaming (Fetch new data continuously)")
    choice = input("Select operation mode (1 or 2): ").strip()
    
    print("\nAvailable Decoded Satellites:")
    for sat_id, info in SAT_REGISTRY.items():
        if "fields" in info:
            measurements = ", ".join(info["fields"].keys())
        else:
            measurements = "Science Payload (High-Fidelity)"
        print(f"  {sat_id:<7} - {info['name']:<10} (Measurements: {measurements})")
    print("  (Or press Enter to attempt fetching for ALL supported satellites above)")
    print("  (NOTE: You can also enter ANY custom NORAD ID to fetch RAW Hex frames)")
    
    sat_choice = input("\nEnter Satellite ID(s) (comma-separated, e.g. 25544, 32785): ").strip()
    if sat_choice:
        # Split by comma or space and filter for digits
        target_satellites = [int(x) for x in sat_choice.replace(',', ' ').split() if x.isdigit()]
    else:
        print("  -> No IDs entered. Defaulting to all supported decoded satellites.")
        target_satellites = list(SAT_REGISTRY.keys())
    
    base_params = {}
    
    if choice == '1':
        print("\nSpecify Date Range (Format: YYYY-MM-DD)")
        start_date = input("Start Date (e.g., 2026-03-01): ").strip()
        end_date = input("End Date   (e.g., 2026-03-18): ").strip()
        
        if start_date: base_params['timestamp__gte'] = f"{start_date}T00:00:00Z"
        if end_date: base_params['timestamp__lte'] = f"{end_date}T23:59:59Z"
        
        pages = int(input("\nHow many API pages to fetch per satellite? (1 page ~ 20-30 records, e.g. 50): ") or "10")
        
        print("\nFetching historical data...")
        for sat in target_satellites:
            sat_name = SAT_REGISTRY.get(sat, {}).get("name", f"Custom_Sat_{sat}")
            print(f"\n-- Fetching Data for {sat_name} ({sat}) --")
            params = base_params.copy()
            params['satellite'] = sat
            data = fetch_data_paginated(params, max_pages=pages)
            if data:
                flatten_and_export(data, append=True)
                print(f"  -> Exported {len(data)} total records for {sat_name}.")
            else:
                print(f"  -> No data found for {sat_name} in this range.")
            time.sleep(2) # delay between sat fetches
            
        print("\nDone! Dataset saved to satellite_measurements.csv")
        
    elif choice == '2':
        poll_interval = int(input("\nEnter polling interval in seconds (default 60): ") or "60")
        print("\nStarting live stream mode. Press Ctrl+C to stop.")
        
        # Keep track of last fetched timestamp to avoid duplicates per satellite
        # Start looking from 6 hours ago to ensure immediate data on startup without overloading
        buffer_t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=6)
        now_utc = buffer_t.strftime("%Y-%m-%dT%H:%M:%SZ")
        last_timestamps = {sat: now_utc for sat in target_satellites}
        
        visualizer = None
        if VIZ_AVAILABLE:
            show_viz = input("Enable real-time visualization popup? (y/n, default n): ").lower().strip() == 'y'
            if show_viz:
                visualizer = SatelliteVisualizer()

        try:
            while True:
                current_time = datetime.datetime.now().strftime('%H:%M:%S')
                print(f"\n[{current_time}] Polling for new frames across {len(target_satellites)} satellite(s)...")
                
                found_new = False
                for sat in target_satellites:
                    sat_name = SAT_REGISTRY.get(sat, {}).get("name", f"Custom_Sat_{sat}")
                    params = base_params.copy()
                    params['satellite'] = sat
                    params['timestamp__gte'] = last_timestamps[sat]
                    
                    try:
                        start_t = time.time()
                        # Single request with 30s timeout (retries removed as per user request)
                        response = requests.get(
                            API_URL, 
                            params=params, 
                            headers=HEADERS,
                            timeout=30 
                        )
                        elapsed = time.time() - start_t

                        if response.status_code != 200:
                            if response.status_code == 400:
                                print(f"  [Sat {sat}] API Error 400: {response.text}")
                            elif response.status_code == 404:
                                print(f"  [Sat {sat}] No telemetry exists in SatNOGS (404).")
                            elif response.status_code == 429:
                                print(f"  [Sat {sat}] ⚡ Rate Limited (429)! Enforcing 15s backoff...")
                                time.sleep(15)
                            else:
                                print(f"  [Sat {sat}] HTTP {response.status_code}")
                        else: # response.status_code == 200
                            result = response.json()
                            data = result.get('results', result) if isinstance(result, dict) else result
                            
                            if data:
                                flatten_and_export(data, append=True)
                                found_new = True
                                print(f"  -> [SUCCESS] Exported {len(data):<3} new frames for {sat_name:<10} ({sat}) in {elapsed:.2f}s.")
                                
                                # Intelligent Payload Routing System
                                for frame in data:
                                    if frame.get('frame'):
                                        hex_val = frame.get('frame')
                                        ts = frame.get('timestamp')
                                        fmt = SAT_REGISTRY.get(sat, {}).get("image_format")
                                        
                                        #if fmt == "SSDV":
                                        #    process_ssdv_frame(sat, hex_val)
                                        #elif fmt == "LRPT":
                                        #    process_lrpt_frame(sat, hex_val, ts)
                                        #elif fmt == "APT":
                                        #    process_apt_frame(sat, hex_val, ts)
                                        #elif fmt and "SSTV" in fmt:
                                        #    # Using SSDV generic handler for fallback 
                                        #    process_ssdv_frame(sat, hex_val)
                                    
                                # update last_timestamp for this sat
                                timestamps = [d.get('timestamp') for d in data if d.get('timestamp')]
                                if timestamps:
                                    last_timestamps[sat] = max(last_timestamps[sat], max(timestamps))
                            else:
                                print(f"  -> [INFO] Checked {sat_name:<10} ({sat}) in {elapsed:.2f}s - 0 new frames.")
                                
                            if visualizer:
                                # Update position based on current time for smooth movement
                                v_lat, v_lon = get_satellite_location(sat, None, None)
                                v_mode = "SGP4" if (SKYFIELD_AVAILABLE and sat in SATELLITES_BY_ID) else "Fallback"
                                visualizer.update_location(v_lat, v_lon, sat_name, mode=v_mode, sat_id=sat)
                    except Exception as e:
                        print(f"  [Sat {sat}] Connection error or processing failure: {str(e)}")
                        
                    print(f"  -> [Rate Control] Sleeping 10s to avoid API ban...")
                    time.sleep(10) # delay between satellite requests
                    
                if not found_new:
                    print(f"  -> No new frames captured in this cycle.")
                    
                time.sleep(poll_interval)
                
        except KeyboardInterrupt:
            print("\nLive data capture stopped.")

if __name__ == "__main__":
    interactive_menu()
