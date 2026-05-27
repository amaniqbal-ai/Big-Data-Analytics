import csv

file_path = "satellite_measurements.csv"

with open(file_path, "r") as file:
    reader = csv.reader(file)

    header = next(reader)
    print("HEADER:", header)

    count = 0

    for row in reader:
        print(row)   # cell-by-cell view
        count += 1

    print("\nTotal Rows:", count)
