import subprocess
import sys
import re

def getUserInput():
    # Check if a file was provided as an argument
    if len(sys.argv) > 1:
        with open(sys.argv[1], 'r') as f:
            return parseInput(f.read())
    else:
        # Prompt the user interactively if no file is provided
        return interactiveInput()

def interactiveInput():
    print("Phi Expression:")
    phiParams = {}
    phiParams['S'] = [val.strip() for val in input("# 1. S - projected columns / expressions (comma separated): \n").split(',')]
    phiParams['n'] = int(input("# 2. number of grouping variables (int): \n"))
    phiParams['V'] = [val.strip() for val in input("# 3. V - grouping attributes (comma separated): \n").split(',')]
    phiParams['F-VECT'] = [val.strip() for val in input("# 4. F-VECT - vector of aggregate functions (comma separated): \n").split(',')]
    phiParams['PRED-LIST'] = [val.strip() for val in input("# 5. PRED-LIST - list of predicates (semicolon separated): \n").split(';')]
    
    having = input('# 6. HAVING (or NONE): \n')
    phiParams['HAVING'] = None if having.upper() == 'NONE' else having
    return phiParams

def parseInput(data):
    # Parses text file for the ESQL query
    data = " ".join(data.split())
    phiParams = {}

    try:
        # 1. Extract S (Select Attributes)
        s_match = re.search(r'SELECT (.*?) FROM', data, re.IGNORECASE)
        phiParams['S'] = [val.strip() for val in s_match.group(1).split(',')] if s_match else []

        # 2. Extract V and n (Grouping Attributes and Count)
        group_match = re.search(r'GROUP BY (.*?) SUCH THAT', data, re.IGNORECASE)
        if group_match:
            group_parts = group_match.group(1).split(';')
            phiParams['V'] = [val.strip() for val in group_parts[0].split(',')]
            variables = [val.strip() for val in group_parts[1].split(',')]
            phiParams['n'] = len(variables)
        else:
            # Handles simple SQL style group by without SUCH THAT
            simple_group_match = re.search(r'GROUP BY (.*?)( ORDER BY| HAVING|$)', data, re.IGNORECASE)
            if simple_group_match:
                phiParams['V'] = [val.strip().rstrip(';') for val in simple_group_match.group(1).split(',')]
            else:
                phiParams['V'] = []
            phiParams['n'] = 0

        # 3. Extract F-VECT (Aggregate Functions)
        phiParams['F-VECT'] = [item for item in phiParams['S'] if '(' in item]

        # 4. Extract PRED-LIST (Predicates / Sigma)
        if 'HAVING' in data.upper():
            pred_match = re.search(r'SUCH THAT (.*?) HAVING', data, re.IGNORECASE)
        else:
            pred_match = re.search(r'SUCH THAT (.*)', data, re.IGNORECASE)
        
        phiParams['PRED-LIST'] = [val.strip() for val in pred_match.group(1).split(',')] if pred_match else []

        # 4.5 Extract simple WHERE condition for non-EMF grouped query path
        where_match = re.search(r'WHERE (.*?)( GROUP BY| ORDER BY| HAVING|$)', data, re.IGNORECASE)
        if where_match:
            phiParams['WHERE'] = where_match.group(1).strip()
        else:
            phiParams['WHERE'] = None

        # 4.6 Extract ORDER BY for simple grouped query path
        order_match = re.search(r'ORDER BY (.*?)( HAVING|$)', data, re.IGNORECASE)
        if order_match:
            phiParams['ORDER BY'] = [val.strip().rstrip(';') for val in order_match.group(1).split(',')]
        else:
            phiParams['ORDER BY'] = None

        # 5. Extract HAVING (G)
        having_match = re.search(r'HAVING (.*)', data, re.IGNORECASE)
        if having_match:
            val = having_match.group(1).strip()
            phiParams['HAVING'] = None if val.upper() == 'NONE' else val
        else:
            phiParams['HAVING'] = None

    except Exception as e:
        print(f"Error parsing input file: {e}")
        sys.exit(1)

    return phiParams

SALES_SCHEMA = {
    "cust": "varchar(20)",
    "prod": "varchar(20)",
    "day": "integer",
    "month": "integer",
    "year": "integer",
    "state": "char(2)",
    "quant": "integer",
    "date": "date"
}

def main():
    phiParams = getUserInput()

    # Prepare aggregate initialization values
    agg_init_dict = {agg: 0 for agg in phiParams['F-VECT']}

    group_attrs_for_simple = phiParams['V'] if len(phiParams['V']) > 0 else ['cust', 'prod']

    # Parse simple aggregate expressions in SELECT, for example avg(quant), max(quant), count(*)
    aggregate_specs = []
    for agg_expr in phiParams['F-VECT']:
        agg_match = re.match(r"^\s*([A-Za-z_]\w*)\s*\(\s*([A-Za-z_][\w\.]*|\*)\s*\)\s*$", agg_expr)
        if agg_match:
            agg_func = agg_match.group(1).lower()
            agg_col = agg_match.group(2)
            if agg_func in ["avg", "max", "min", "sum", "count"]:
                aggregate_specs.append({
                    "expr": agg_expr,
                    "func": agg_func,
                    "col": agg_col
                })

    # Parse basic WHERE predicates joined by AND
    where_conditions = []
    if phiParams.get('WHERE'):
        where_parts = re.split(r"\s+and\s+", phiParams['WHERE'], flags=re.IGNORECASE)
        for part in where_parts:
            cond_match = re.match(r"^\s*([A-Za-z_]\w*)\s*(=|!=|>=|<=|>|<)\s*('?[^']*'?|\d+)\s*$", part.strip())
            if cond_match:
                left_col = cond_match.group(1)
                operator = cond_match.group(2)
                right_raw = cond_match.group(3).strip()
                if right_raw.startswith("'") and right_raw.endswith("'"):
                    right_value = right_raw[1:-1]
                    right_type = "str"
                else:
                    right_value = int(right_raw)
                    right_type = "num"
                where_conditions.append({
                    "left": left_col,
                    "op": operator,
                    "right": right_value,
                    "right_type": right_type
                })

    # This simple query path is enabled only when input is not using SUCH THAT
    simple_query_mode = len([p for p in phiParams['PRED-LIST'] if p]) == 0 and len(group_attrs_for_simple) > 0 and len(aggregate_specs) > 0
    
    # Renamed variable to table_scan_1_code to match the document 
    table_scan_1_code = f"""
    cur.execute("SELECT * FROM sales")
    for row in cur:
        # Create a unique key for the group based on attributes V: {phiParams['V']}
        key = tuple(row[attr] for attr in {phiParams['V']})
        if key not in mf_struct:
            # Initialize aggregate variables: {phiParams['F-VECT']}
            mf_struct[key] = {agg_init_dict}
    """

    # Build generated code blocks for aggregate initialization, updates, and output values
    agg_init_lines = []
    agg_update_lines = []
    agg_output_lines = []

    for spec in aggregate_specs:
        expr = spec["expr"]
        func = spec["func"]
        col = spec["col"]
        safe_name = expr.replace("(", "_").replace(")", "").replace(".", "_").replace("*", "star").replace(" ", "")

        if func == "sum":
            agg_init_lines.append(f"                    '{safe_name}': 0,")
            if col == "*":
                agg_update_lines.append(f"            grouped_results[group_key]['{safe_name}'] = grouped_results[group_key]['{safe_name}'] + 1")
            else:
                agg_update_lines.append(f"            grouped_results[group_key]['{safe_name}'] = grouped_results[group_key]['{safe_name}'] + row['{col}']")
            agg_output_lines.append(f"            output_row['{expr}'] = group_data['{safe_name}']")

        elif func == "count":
            agg_init_lines.append(f"                    '{safe_name}': 0,")
            agg_update_lines.append(f"            grouped_results[group_key]['{safe_name}'] = grouped_results[group_key]['{safe_name}'] + 1")
            agg_output_lines.append(f"            output_row['{expr}'] = group_data['{safe_name}']")

        elif func == "max":
            if col == "*":
                agg_init_lines.append(f"                    '{safe_name}': None,")
                agg_update_lines.append(f"            if grouped_results[group_key]['{safe_name}'] is None or 1 > grouped_results[group_key]['{safe_name}']:")
                agg_update_lines.append(f"                grouped_results[group_key]['{safe_name}'] = 1")
            else:
                agg_init_lines.append(f"                    '{safe_name}': None,")
                agg_update_lines.append(f"            if grouped_results[group_key]['{safe_name}'] is None or row['{col}'] > grouped_results[group_key]['{safe_name}']:")
                agg_update_lines.append(f"                grouped_results[group_key]['{safe_name}'] = row['{col}']")
            agg_output_lines.append(f"            output_row['{expr}'] = group_data['{safe_name}']")

        elif func == "min":
            if col == "*":
                agg_init_lines.append(f"                    '{safe_name}': None,")
                agg_update_lines.append(f"            if grouped_results[group_key]['{safe_name}'] is None or 1 < grouped_results[group_key]['{safe_name}']:")
                agg_update_lines.append(f"                grouped_results[group_key]['{safe_name}'] = 1")
            else:
                agg_init_lines.append(f"                    '{safe_name}': None,")
                agg_update_lines.append(f"            if grouped_results[group_key]['{safe_name}'] is None or row['{col}'] < grouped_results[group_key]['{safe_name}']:")
                agg_update_lines.append(f"                grouped_results[group_key]['{safe_name}'] = row['{col}']")
            agg_output_lines.append(f"            output_row['{expr}'] = group_data['{safe_name}']")

        elif func == "avg":
            avg_sum_name = safe_name + "_sum"
            avg_count_name = safe_name + "_count"
            agg_init_lines.append(f"                    '{avg_sum_name}': 0,")
            agg_init_lines.append(f"                    '{avg_count_name}': 0,")
            if col == "*":
                agg_update_lines.append(f"            grouped_results[group_key]['{avg_sum_name}'] = grouped_results[group_key]['{avg_sum_name}'] + 1")
            else:
                agg_update_lines.append(f"            grouped_results[group_key]['{avg_sum_name}'] = grouped_results[group_key]['{avg_sum_name}'] + row['{col}']")
            agg_update_lines.append(f"            grouped_results[group_key]['{avg_count_name}'] = grouped_results[group_key]['{avg_count_name}'] + 1")
            agg_output_lines.append(f"            if group_data['{avg_count_name}'] > 0:")
            agg_output_lines.append(f"                output_row['{expr}'] = group_data['{avg_sum_name}'] / group_data['{avg_count_name}']")
            agg_output_lines.append("            else:")
            agg_output_lines.append(f"                output_row['{expr}'] = 0")

    where_filter_lines = ["        should_use_row = True"]
    for cond in where_conditions:
        op = "==" if cond["op"] == "=" else cond["op"]
        if cond["right_type"] == "str":
            where_filter_lines.append(f"        if not (row['{cond['left']}'] {op} '{cond['right']}'):")
            where_filter_lines.append("            should_use_row = False")
        else:
            where_filter_lines.append(f"        if not (row['{cond['left']}'] {op} {cond['right']}):")
            where_filter_lines.append("            should_use_row = False")

    order_by_cols = phiParams['ORDER BY'] if phiParams.get('ORDER BY') else group_attrs_for_simple

    table_scan_2_code = f"""
    grouped_results = {{}}

    cur.execute("SELECT * FROM sales")
    for row in cur:
{chr(10).join(where_filter_lines)}
        # Use current row when it matches the parsed filter condition
        if should_use_row:
            # Build the group key using parsed grouping attributes
            group_key = tuple(row[attr] for attr in {group_attrs_for_simple})

            # Create a new group entry the first time we see this key
            if group_key not in grouped_results:
                grouped_results[group_key] = {{
{chr(10).join(agg_init_lines)}
                }}

{chr(10).join(agg_update_lines)}
    """

    tmp = f"""
import os
import psycopg2
import psycopg2.extras
import tabulate
from dotenv import load_dotenv

# DO NOT EDIT THIS FILE, IT IS GENERATED BY generator.py

def query():
    load_dotenv()
    # ... (connection setup) ...
    conn = psycopg2.connect(dbname=os.getenv('DBNAME'), 
                            user=os.getenv('USER'), 
                            password=os.getenv('PASSWORD'),
                            cursor_factory=psycopg2.extras.DictCursor)
    cur = conn.cursor()
    
    mf_struct = {{}}
    
    # --- TABLE SCAN 1: populate mf-struct with distinct values of grouping attribute (V) ---
    {table_scan_1_code}
    
    # --- TABLE SCAN 2: simple grouped processing ---
    {table_scan_2_code}
    
    # --- FUTURE MF/EMF SCANS WILL GO HERE ---
    output = []
    if {simple_query_mode}:
        for group_key in grouped_results:
            group_data = grouped_results[group_key]

            output_row = {{}}
            for i, attr_name in enumerate({group_attrs_for_simple}):
                output_row[attr_name] = group_key[i]

{chr(10).join(agg_output_lines)}
            output.append(output_row)

        # Sort grouped output so generated result is stable for testing
        output.sort(key=lambda row: tuple(row[attr_name] for attr_name in {order_by_cols}))
    else:
        for key, aggs in mf_struct.items():
            row = {{attr: key[i] for i, attr in enumerate({phiParams['V']})}}
            row.update(aggs)
            output.append(row)
        
    return tabulate.tabulate(output, headers="keys", tablefmt="psql")
    

if "__main__" == __name__:
    print(query())
    """

    open("_generated.py", "w").write(tmp)
    subprocess.run([sys.executable, "_generated.py"])

if "__main__" == __name__:
    main()