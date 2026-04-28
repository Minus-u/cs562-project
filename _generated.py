
import os
import psycopg2
import psycopg2.extras
import tabulate
from dotenv import load_dotenv

def query():
    load_dotenv()
    conn = psycopg2.connect(dbname=os.getenv('DBNAME'), user=os.getenv('USER'), 
                            password=os.getenv('PASSWORD'), cursor_factory=psycopg2.extras.DictCursor)
    cur = conn.cursor()
    mf_struct = {}

    # --- SCAN 1: Discovery & Filtering ---
    cur.execute("SELECT * FROM sales")
    for row in cur:
        if True:
            key = tuple(row[attr] for attr in ['prod'])
            if key not in mf_struct:
                mf_struct[key] = {'avg_ny_quant__sum': 0, 'avg_ny_quant__count': 0, 'sum_ny_quant': 0}.copy()

    # --- SCANS 2 to 2: Processing ---
    # --- TABLE SCAN 2: Processing Grouping Variable ny ---
    cur.scroll(0, mode='absolute')
    for row in cur:
        key = tuple(row[attr] for attr in ['prod'])
        if key in mf_struct:
            if row['state'] == 'NJ':
                mf_struct[key]['avg_ny_quant__sum'] += row['quant']
                mf_struct[key]['avg_ny_quant__count'] += 1
                mf_struct[key]['sum_ny_quant'] += row['quant']



    # --- OUTPUT ---
    output = []
    # Converts internal mf_struct data into a list of row dictionaries for tabulate.
    for key, aggs in mf_struct.items():
        row = {attr: key[j] for j, attr in enumerate(['prod'])}
        for name, val in aggs.items():
            if name.endswith("__sum"):
                base = name[:-5]
                count_name = base + "__count"
                row[base] = val / aggs[count_name] if aggs[count_name] > 0 else 0
            elif not name.endswith("__count"):
                row[name] = val
        output.append(row)
    
    return tabulate.tabulate(output, headers="keys", tablefmt="psql")

if __name__ == "__main__":
    print(query())
    