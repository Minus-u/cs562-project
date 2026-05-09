
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

    # TABLE SCAN 1: populate mf-struct with distinct values of grouping attribute (V)
    cur.execute("SELECT * FROM sales")
    for row in cur:
        if True:
            key = tuple(row[attr] for attr in ['cust'])
            if key not in mf_struct:
                mf_struct[key] = {'1_sum_quant': 0, '1_avg_quant__sum': 0, '1_avg_quant__count': 0, '2_sum_quant': 0, '3_sum_quant': 0, '3_avg_quant__sum': 0, '3_avg_quant__count': 0}.copy()

    # SCANS 2 to 4: Processing 
    # MF SCAN FOR GROUPING VARIABLE 1
    cur.scroll(0, mode='absolute')
    for row in cur:
        key = tuple(row[attr] for attr in ['cust'])
        if key in mf_struct:
            if row['state']=='NY':
                mf_struct[key]['1_sum_quant'] += row['quant']
                mf_struct[key]['1_avg_quant__sum'] += row['quant']
                mf_struct[key]['1_avg_quant__count'] += 1

    # MF SCAN FOR GROUPING VARIABLE 2
    cur.scroll(0, mode='absolute')
    for row in cur:
        key = tuple(row[attr] for attr in ['cust'])
        if key in mf_struct:
            if row['state']=='NJ':
                mf_struct[key]['2_sum_quant'] += row['quant']

    # MF SCAN FOR GROUPING VARIABLE 3
    cur.scroll(0, mode='absolute')
    for row in cur:
        key = tuple(row[attr] for attr in ['cust'])
        if key in mf_struct:
            if row['state']=='CT':
                mf_struct[key]['3_sum_quant'] += row['quant']
                mf_struct[key]['3_avg_quant__sum'] += row['quant']
                mf_struct[key]['3_avg_quant__count'] += 1



    # OUTPUT
    output = []
    # Converts internal mf_struct data into a list of row dictionaries for tabulate.
    for key, aggs in mf_struct.items():
        row = {attr: key[j] for j, attr in enumerate(['cust'])}
        for name, val in aggs.items():
            
            # Reconstruct 'avg' by dividing the internal __sum by __count
            if name.endswith("__sum"):
                base = name[:-5]
                count_name = base + "__count"
                row[base] = val / aggs[count_name] if aggs[count_name] > 0 else 0
                
            # Keep normal aggregates, but hide the internal __count tracking variables
            elif not name.endswith("__count"):
                row[name] = val
                
        # APPLY HAVING CLAUSE
        if row['1_sum_quant'] > 2 * row['2_sum_quant'] or row['1_avg_quant'] > row['3_avg_quant']:
            output.append(row)
        
    # APPLY ORDER BY
    sort_cols = []
    if sort_cols:
        output.sort(key=lambda row: tuple(row.get(col, 0) for col in sort_cols))
    
    return tabulate.tabulate(output, headers="keys", tablefmt="psql")

if __name__ == "__main__":
    print(query())
    