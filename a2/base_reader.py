import csv

file_path = 'satellite_measurements.csv'

def read_satellite_data(path):
    try:
        with open(path, mode='r', encoding='utf-8') as file:
            # csv.reader automatically tokenizes by comma (cell by cell)
            reader = csv.reader(file)
            for line_no, tokens in enumerate(reader):
                # 'tokens' is a list of strings (cells)
                print(f"Line {line_no}: {tokens}")
                
                # To view in entirety without crashing terminal, 
                # you might want to limit this to the first 10 lines
                if line_no > 10: 
                    print("... (continuing to read file) ...")
                    break 
    except FileNotFoundError:
        print("File not found. Ensure satellite_measurements.csv is in the directory.")

if __name__ == "__main__":
    read_satellite_data(file_path)
