import subprocess
import sys
import re

# Hard-coded schema data for the 'sales' table
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

# helper functions

# Convert SQL equality and logical operators to python equivalents
def translate_sql_operators(condition_string):
    if not condition_string:
        return "True"
    # convert SQL '=' to Python '==' while preserving existing operators (<=, >=, !=)
    python_cond = re.sub(r"(?<![<>!=])=(?!=)", "==", condition_string)
    # convert SQL 'AND' and 'OR' with python 'and' and 'or'
    python_cond = python_cond.replace("AND", "and").replace("OR", "or")
    return python_cond

# translate column names to row dictionary access: state = 'NY' -> row['state'] == 'NY'
def translate_where_clause(where_string, columns):
    if not where_string:
        return "True"
    python_filter = translate_sql_operators(where_string)
    for col in columns:
        python_filter = re.sub(rf"\b{col}\b", f"row['{col}']", python_filter)
    return python_filter

# translate grouping variable prefixes (1,2,3...) to row dictionary access: 1.state -> row['state']
def translate_such_that_predicate(predicate_string, grouping_var):
    # if no condition attached or "TRUE", don't filter
    if not predicate_string or predicate_string.upper() == "TRUE":
        return "True"
    # convert SQL to python code
    python_pred = translate_sql_operators(predicate_string)
    # replace the variable prefixes with the actual grouping variable name
    python_pred = re.sub(rf"\b{grouping_var}\.([a-zA-Z_]\w*)", r"row['\1']", python_pred)
    return python_pred

# translates HAVING logic to row dictionary access for the final output filter
def translate_having_clause(having_string, aggregates, grouping_attrs):
    if not having_string or having_string.upper() == "NONE":
        return "True"
    
    python_having = translate_sql_operators(having_string)
    
    # Use placeholders to prevent double-replacement bugs
    for agg in sorted(aggregates, key=len, reverse=True):
        agg_name = makeAggregateName(agg)
        # 1. Replace raw SQL format (e.g., sum(1.quant)) with placeholder
        python_having = python_having.replace(agg, f"__TMP_{agg_name}__")
        # 2. Replace relational algebra format (e.g., sum_1_quant) with placeholder
        python_having = re.sub(rf"\b{agg_name}\b", f"__TMP_{agg_name}__", python_having)
        
        # 3. Handle swapped formats (e.g. 1_sum_quant vs sum_1_quant)
        parts = agg_name.split("_")
        if len(parts) == 3:
            alt_name = f"{parts[1]}_{parts[0]}_{parts[2]}"
            python_having = re.sub(rf"\b{alt_name}\b", f"__TMP_{agg_name}__", python_having)
            
    # Convert placeholders to final row dictionary access
    python_having = re.sub(r"__TMP_([a-zA-Z0-9_]+)__", r"row['\1']", python_having)
        
    # Replace grouping attributes with dictionary lookups (e.g., cust == 'Sam')
    # The negative lookbehinds (?<!['"]) ensure we don't double-replace strings already inside row['']
    for v in grouping_attrs:
        python_having = re.sub(rf"(?<!['\"])\b{v}\b(?!['\"])", f"row['{v}']", python_having)
        
    return python_having

# generates the code string needed to update an aggregate during a table scan
def generate_aggregate_update_code(func_type, agg_name, column_name):
    func = func_type.lower()
    if "count" in func: 
        return f"mf_struct[key]['{agg_name}'] += 1"
    elif "sum" in func: 
        return f"mf_struct[key]['{agg_name}'] += row['{column_name}']"
    elif "avg" in func:
        # Averages are split into sum and count components for internal calculation
        return (f"mf_struct[key]['{agg_name}__sum'] += row['{column_name}']\n"
                f"            mf_struct[key]['{agg_name}__count'] += 1")
    elif "max" in func:
        return f"if mf_struct[key]['{agg_name}'] is None or row['{column_name}'] > mf_struct[key]['{agg_name}']: mf_struct[key]['{agg_name}'] = row['{column_name}']"
    elif "min" in func:
        return f"if mf_struct[key]['{agg_name}'] is None or row['{column_name}'] < mf_struct[key]['{agg_name}']: mf_struct[key]['{agg_name}'] = row['{column_name}']"
    return "pass"

def makeAggregateName(expr):
    # Change SQL aggregate functions to be in a neat format to be used as a key: avg(x.quant) -> avg_x_quant
    return expr.replace("(", "_").replace(")", "").replace(".", "_").replace("*", "star").replace(" ", "")

# parsing and input logic
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
    
    # Names grouping variables 1, 2, 3 to match the relational algebra input style as shown in the Overall Logic document
    phiParams['GROUPING_VARIABLES'] = [str(i+1) for i in range(phiParams['n'])]
    
    having = input('# 6. HAVING (or NONE): \n')
    phiParams['HAVING'] = None if having.upper() == 'NONE' else having
    
    print('Additional clauses:')

    order_by = input('# 7. ORDER BY (or NONE, comma separated): \n')
    phiParams['ORDER BY'] = None if order_by.upper() == 'NONE' else [val.strip() for val in order_by.split(',')]
    
    return phiParams

def parseInput(data):
    # Parses text file for the ESQL query (removes all whitespace and newlines)
    data = " ".join(data.split())
    phiParams = {}
    try:
        # regex to get the projected columns from "SELECT" to "FROM"
        selectClause = re.search(r'SELECT (.*?) FROM', data, re.IGNORECASE)
        # regex to split the columns into a list
        phiParams['S'] = [val.strip() for val in selectClause.group(1).split(',')] if selectClause else []
        
        # regex to get the projected columns from "WHERE" to "GROUP BY" or "ORDER BY" or "HAVING" or the end of the query
        whereClause = re.search(r'WHERE (.*?)(?: GROUP BY| HAVING| ORDER BY|$)', data, re.IGNORECASE)
        phiParams['WHERE'] = whereClause.group(1).strip() if whereClause else None

        # regex to get the grouping variables and grouping attributes, from "GROUP BY" to "SUCH THAT" or "HAVING" or "ORDER BY" or the end of the query
        groupByClause = re.search(r'GROUP\s+BY\s+(.*?)(?:\s+SUCH\s+THAT|\s+HAVING|\s+ORDER\s+BY|$)', data, re.IGNORECASE)
        if groupByClause:
            groupText = groupByClause.group(1).strip()
            if ';' in groupText:
                # MF/EMF format: GROUP BY cust; x, y, z
                groupSections = groupText.split(';', 1)
                phiParams['V'] = [val.strip() for val in groupSections[0].split(',')]
                phiParams['GROUPING_VARIABLES'] = [val.strip() for val in groupSections[1].split(',')]
                phiParams['n'] = len(phiParams['GROUPING_VARIABLES'])
            else:
                # Simple SQL format: GROUP BY cust, prod
                phiParams['V'] = [val.strip() for val in groupText.split(',')]
                phiParams['GROUPING_VARIABLES'] = []
                phiParams['n'] = 0
        else:
            phiParams['V'] = []
            phiParams['GROUPING_VARIABLES'] = []
            phiParams['n'] = 0
        
        # initalize the vector of aggregate functions based off the aggregate functions in the SELECT clause
        phiParams['F-VECT'] = [item for item in phiParams['S'] if '(' in item or '_' in item]
        
        # regex to get the list of predicates for grouping variables, from "SUCH THAT" to "HAVING", "ORDER BY" or end of the query
        suchThatClause = re.search(r'SUCH\s+THAT\s+(.*?)(?:\s+HAVING|\s+ORDER\s+BY|$)', data, re.IGNORECASE)
        phiParams['PRED-LIST'] = [val.strip() for val in suchThatClause.group(1).split(',')] if suchThatClause else []
            
        # regex to get the list of predicates for "HAVING", till "ORDER BY" or the end of the query
        havingClause = re.search(r'HAVING (.*?)(?:\s+ORDER\s+BY|$)', data, re.IGNORECASE)
        phiParams['HAVING'] = None if not havingClause or havingClause.group(1).strip().upper() == 'NONE' else havingClause.group(1).strip()
        
        # regex to get the list of attributes to "ORDER BY"
        orderByClause = re.search(r'ORDER\s+BY\s+(.*?)(?:;|$)', data, re.IGNORECASE)
        phiParams['ORDER BY'] = [val.strip() for val in orderByClause.group(1).split(',')] if orderByClause else None

    except Exception as e:
        print(f"Error parsing input: {e}")
        sys.exit(1)
    return phiParams

# main awesome generator
def main():
    phiParams = getUserInput()

    # Prepare aggregate initialization values
    initialAggregates = {}
    for agg in phiParams['F-VECT']:
        name = makeAggregateName(agg)
        
        # Split avg into sum and count functions, and compute avg later
        if "avg" in agg.lower():
            initialAggregates[name + "__sum"] = 0
            initialAggregates[name + "__count"] = 0
            
        # Aggregates start at 0 unless max() or min(), then None
        elif "max" in agg.lower() or "min" in agg.lower():
            initialAggregates[name] = None
        else:
            initialAggregates[name] = 0

    # Parse basic WHERE predicates
    schema_columns = list(SALES_SCHEMA.keys())
    where_filter = translate_where_clause(phiParams.get('WHERE'), schema_columns)
    
    # Parse HAVING predicate logic
    python_having = translate_having_clause(phiParams.get('HAVING'), phiParams['F-VECT'], phiParams['V'])

    # Generates Scan 1 code (Discovery & immediate aggregation if n=0)
    scan1_updates = []
    if phiParams['n'] == 0:
        for agg in phiParams['F-VECT']:
            name = makeAggregateName(agg)
            
            # Determine the function and column based on the aggregate format
            if '(' in agg:
                # Handles format like "sum(quant)"
                func = agg.split('(')[0]                 # Extracts "sum"
                col = re.search(r"\((.*?)\)", agg).group(1) # Extracts "quant"
            else:
                # Handles format like "1_sum_quant"
                func = agg.split('_')[1]                 # Extracts "sum"
                col = agg.split('_')[-1]                 # Extracts "quant"
            
            # Generate the specific math string and save it for the loop
            update_code = generate_aggregate_update_code(func, name, col)
            if update_code != "pass":
                for line in update_code.split('\n'):
                    scan1_updates.append(line.strip())

    tableScan1Lines = [
        'cur.execute("SELECT * FROM sales")',
        'for row in cur:',
        f'    if {where_filter}:',
        f'        key = tuple(row[attr] for attr in {phiParams["V"]})',
        '        if key not in mf_struct:',
        f'            mf_struct[key] = {initialAggregates}.copy()'
    ]
    if scan1_updates:
        for line in scan1_updates:
            tableScan1Lines.append(f'        {line}')
    tableScan1 = "\n".join(["    " + line for line in tableScan1Lines])

    # Iterates through n grouping variables to generate individual table scan loops
    multiScanCode = ""
    for i in range(phiParams['n']):
        gv = phiParams['GROUPING_VARIABLES'][i]
        
        # 1. Get the predicate for this grouping variable
        pred = phiParams['PRED-LIST'][i] if i < len(phiParams['PRED-LIST']) else "True"
        python_pred = translate_such_that_predicate(pred, gv)
        
        # 2. Translates dependent aggregate references into lookups (for EMF queries)
        for agg in phiParams['F-VECT']:
            agg_name = makeAggregateName(agg)
            if agg_name in python_pred:
                # AVG seperated into SUM and COUNT
                if "avg" in agg.lower():
                    avg_calc = f"(mf_struct[key]['{agg_name}__sum'] / mf_struct[key]['{agg_name}__count'] if mf_struct[key]['{agg_name}__count'] > 0 else 0)"
                    python_pred = python_pred.replace(agg_name, avg_calc)
                else:
                    python_pred = python_pred.replace(agg_name, f"mf_struct[key]['{agg_name}']")

        # 3. Determine which aggregates are updated during the current table scan
        update_logic = []
        for agg in phiParams['F-VECT']:
            name = makeAggregateName(agg)
            
            # Only generate update code if the aggregate belongs to the current grouping variable
            if agg.startswith(f"{gv}_") or f"({gv}." in agg:
                
                # Determine the function and column based on the aggregate format
                if '(' in agg:
                    # Handles ESQL format like "sum(1.quant)"
                    func = agg.split('(')[0].lower()
                    col = re.search(r"\((.*?)\)", agg).group(1).split('.')[-1]
                else:
                    # Handles Relational Algebra format like "1_sum_quant"
                    func = agg.split('_')[1].lower()
                    col = agg.split('_')[-1]

                # Generate the specific math string and save it for the loop
                update_code = generate_aggregate_update_code(func, name, col)
                if update_code != "pass":
                    for line in update_code.split('\n'):
                        update_logic.append(line.strip())
        
        if not update_logic: 
            update_logic.append("pass")

        # 4. Assemble the Python loop for this specific scan
        scan_lines = [
            f"# MF SCAN FOR GROUPING VARIABLE {gv}",
            "cur.scroll(0, mode='absolute')",
            "for row in cur:",
            f"    key = tuple(row[attr] for attr in {phiParams['V']})",
            "    if key in mf_struct:",
            f"        if {python_pred}:"
        ]
        
        # Inject the math update logic into the if statement
        for line in update_logic: 
            scan_lines.append(f"            {line}")
            
        # Stitch it all into a multi-line string
        multiScanCode += "\n".join(["    " + line for line in scan_lines]) + "\n\n"

    # Sanitize ORDER BY columns so they match internal dictionary keys
    raw_order_by = phiParams.get('ORDER BY') or []
    clean_order_by = [makeAggregateName(col) for col in raw_order_by]

    # Writes the finalized Python code for _generated.py
    generatedProgram = f"""
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
    mf_struct = {{}}

    # TABLE SCAN 1: populate mf-struct with distinct values of grouping attribute (V)
{tableScan1}

    # SCANS 2 to {phiParams['n']+1}: Processing 
{multiScanCode}

    # OUTPUT
    output = []
    # Converts internal mf_struct data into a list of row dictionaries for tabulate.
    for key, aggs in mf_struct.items():
        row = {{attr: key[j] for j, attr in enumerate({phiParams['V']})}}
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
        if {python_having}:
            output.append(row)
        
    # APPLY ORDER BY
    sort_cols = {clean_order_by}
    if sort_cols:
        output.sort(key=lambda row: tuple(row.get(col, 0) for col in sort_cols))
    
    return tabulate.tabulate(output, headers="keys", tablefmt="psql")

if __name__ == "__main__":
    print(query())
    """
    
    # Saves the generated program and executes it as a sub-process.
    with open("_generated.py", "w") as f:
        f.write(generatedProgram)
    subprocess.run([sys.executable, "_generated.py"])

if __name__ == "__main__":
    main()