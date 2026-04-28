import subprocess
import sys
import re
import os

# Selects between file-based or interactive input modes.
def getUserInput():
    if len(sys.argv) > 1:
        with open(sys.argv[1], 'r') as f:
            return parseInput(f.read())
    else:
        return interactiveInput()

# Prompts the user for the 6 components of the Phi operator.
def interactiveInput():
    print("Phi Expression:")
    phiParams = {}
    phiParams['S'] = [val.strip() for val in input("# 1. S - projected columns / expressions (comma separated): \n").split(',')]
    phiParams['n'] = int(input("# 2. number of grouping variables (int): \n"))
    phiParams['V'] = [val.strip() for val in input("# 3. V - grouping attributes (comma separated): \n").split(',')]
    phiParams['F-VECT'] = [val.strip() for val in input("# 4. F-VECT - vector of aggregate functions (comma separated): \n").split(',')]
    phiParams['PRED-LIST'] = [val.strip() for val in input("# 5. PRED-LIST - list of predicates (semicolon separated): \n").split(';')]
    
    # Names grouping variables 1, 2, 3 to match the relational algebra input style.
    phiParams['GROUPING_VARIABLES'] = [str(i+1) for i in range(phiParams['n'])]
    
    having = input('# 6. HAVING (or NONE): \n')
    phiParams['HAVING'] = None if having.upper() == 'NONE' else having
    return phiParams

# Uses regular expressions to extract query components from a raw ESQL text file.
def parseInput(data):
    data = " ".join(data.split())
    phiParams = {}
    try:
        selectClause = re.search(r'SELECT (.*?) FROM', data, re.IGNORECASE)
        phiParams['S'] = [val.strip() for val in selectClause.group(1).split(',')] if selectClause else []
        
        # Extracts the standard WHERE clause for initial filtering.
        whereClause = re.search(r'WHERE (.*?)(?: GROUP BY| HAVING|$)', data, re.IGNORECASE)
        phiParams['WHERE'] = whereClause.group(1).strip() if whereClause else None

        groupByClause = re.search(r'GROUP\s+BY\s+(.*?)(?:\s+SUCH\s+THAT|\s+HAVING|$)', data, re.IGNORECASE)
        if groupByClause:
            groupText = groupByClause.group(1).strip()
            if ';' in groupText:
                groupSections = groupText.split(';', 1)
                phiParams['V'] = [val.strip() for val in groupSections[0].split(',')]
                phiParams['GROUPING_VARIABLES'] = [val.strip() for val in groupSections[1].split(',')]
                phiParams['n'] = len(phiParams['GROUPING_VARIABLES'])
            else:
                phiParams['V'] = [val.strip() for val in groupText.split(',')]
                phiParams['GROUPING_VARIABLES'] = []
                phiParams['n'] = 0
        
        # Identifies aggregates by looking for parentheses or the underscore naming convention.
        phiParams['F-VECT'] = [item for item in phiParams['S'] if '(' in item or '_' in item]
        
        suchThatClause = re.search(r'SUCH\s+THAT\s+(.*?)(?:\s+HAVING|$)', data, re.IGNORECASE)
        phiParams['PRED-LIST'] = [val.strip() for val in suchThatClause.group(1).split(',')] if suchThatClause else []
            
        havingClause = re.search(r'HAVING (.*)', data, re.IGNORECASE)
        phiParams['HAVING'] = None if not havingClause or havingClause.group(1).strip().upper() == 'NONE' else havingClause.group(1).strip()
    except Exception as e:
        print(f"Error parsing input: {e}")
        sys.exit(1)
    return phiParams

# Sanitizes aggregate strings to create valid Python dictionary keys.
def makeAggregateName(expr):
    return expr.replace("(", "_").replace(")", "").replace(".", "_").replace("*", "star").replace(" ", "")

def main():
    phiParams = getUserInput()

    # Initializes aggregate values to zero, splitting 'avg' into sum and count components.
    initialAggregates = {}
    for agg in phiParams['F-VECT']:
        name = makeAggregateName(agg)
        if "avg" in agg.lower():
            initialAggregates[name + "__sum"] = 0
            initialAggregates[name + "__count"] = 0
        else:
            initialAggregates[name] = 0

    # Translates the global WHERE clause into Python row-access logic.
    where_filter = "True"
    if phiParams.get('WHERE'):
        where_filter = re.sub(r"(?<![<>!=])=(?!=)", "==", phiParams['WHERE'])
        where_filter = where_filter.replace("AND", "and").replace("OR", "or")
        for key in ["cust", "prod", "day", "month", "year", "state", "quant"]:
            where_filter = re.sub(rf"\b{key}\b", f"row['{key}']", where_filter)

    # Identifies standard SQL aggregates to be processed during the first scan.
    scan1_updates = []
    if phiParams['n'] == 0:
        for agg in phiParams['F-VECT']:
            name = makeAggregateName(agg)
            col = re.search(r"\((.*?)\)", agg).group(1) if '(' in agg else agg.split('_')[-1]
            
            if "count" in agg.lower():
                scan1_updates.append(f"mf_struct[key]['{name}'] += 1")
            elif "sum" in agg.lower():
                scan1_updates.append(f"mf_struct[key]['{name}'] += row['{col}']")
            elif "avg" in agg.lower():
                scan1_updates.append(f"mf_struct[key]['{name}__sum'] += row['{col}']")
                scan1_updates.append(f"mf_struct[key]['{name}__count'] += 1")
            elif "max" in agg.lower():
                scan1_updates.append(f"if mf_struct[key]['{name}'] is None or row['{col}'] > mf_struct[key]['{name}']: mf_struct[key]['{name}'] = row['{col}']")

    # Generates Scan 1 code with filtering and immediate aggregation for n=0.
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

    # Iterates through n grouping variables to generate individual table scan loops.
    multiScanCode = ""
    for i in range(phiParams['n']):
        gv = phiParams['GROUPING_VARIABLES'][i]
        pred = phiParams['PRED-LIST'][i] if i < len(phiParams['PRED-LIST']) else "True"
        
        # Safely converts SQL '=' to Python '==' while preserving existing operators.
        python_pred = re.sub(r"(?<![<>!=])=(?!=)", "==", pred)
        python_pred = python_pred.replace("AND", "and").replace("OR", "or")
        
        # Translates grouping variable prefixes into current row column access.
        python_pred = re.sub(rf"\b{gv}\.([a-zA-Z_]\w*)", r"row['\1']", python_pred)
        
        # Translates dependent aggregate references into lookups, including inline average math.
        for agg in phiParams['F-VECT']:
            agg_name = makeAggregateName(agg)
            if agg_name in python_pred:
                if "avg" in agg.lower():
                    avg_calc = f"(mf_struct[key]['{agg_name}__sum'] / mf_struct[key]['{agg_name}__count'] if mf_struct[key]['{agg_name}__count'] > 0 else 0)"
                    python_pred = python_pred.replace(agg_name, avg_calc)
                else:
                    python_pred = python_pred.replace(agg_name, f"mf_struct[key]['{agg_name}']")

        # Determines which aggregates are updated during the current table scan.
        update_logic = []
        for agg in phiParams['F-VECT']:
            name = makeAggregateName(agg)
            is_match = False
            # Supports both '1_sum_quant' and 'sum(1.quant)' input formats.
            if agg.startswith(f"{gv}_") or f"({gv}." in agg:
                is_match = True
                col = re.search(r"\((.*?)\)", agg).group(1).split('.')[-1] if '(' in agg else agg.split('_')[-1]
                func = agg.split('(')[0].lower() if '(' in agg else agg.split('_')[1].lower()

            # Adds logic to update sums, counts, averages, and maximum values.
            if is_match:
                if "count" in func: update_logic.append(f"mf_struct[key]['{name}'] += 1")
                elif "sum" in func: update_logic.append(f"mf_struct[key]['{name}'] += row['{col}']")
                elif "avg" in func:
                    update_logic.append(f"mf_struct[key]['{name}__sum'] += row['{col}']")
                    update_logic.append(f"mf_struct[key]['{name}__count'] += 1")
                elif "max" in func:
                    update_logic.append(f"if row['{col}'] > mf_struct[key]['{name}']: mf_struct[key]['{name}'] = row['{col}']")
        
        # Prevents IndentationError by adding 'pass' if no aggregates match the scan.
        if not update_logic: update_logic.append("pass")

        # Assembles the scan block with absolute cursor scrolling and group key lookups.
        scan_lines = [
            f"# --- TABLE SCAN {i+2}: Processing Grouping Variable {gv} ---",
            "cur.scroll(0, mode='absolute')",
            "for row in cur:",
            f"    key = tuple(row[attr] for attr in {phiParams['V']})",
            "    if key in mf_struct:",
            f"        if {python_pred}:"
        ]
        for line in update_logic: scan_lines.append(f"            {line}")
        multiScanCode += "\n".join(["    " + line for line in scan_lines]) + "\n\n"

    # Writes the finalized Python code for _generated.py using a formatted multi-line string.
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

    # --- SCAN 1: Discovery & Filtering ---
{tableScan1}

    # --- SCANS 2 to {phiParams['n']+1}: Processing ---
{multiScanCode}

    # --- OUTPUT ---
    output = []
    # Converts internal mf_struct data into a list of row dictionaries for tabulate.
    for key, aggs in mf_struct.items():
        row = {{attr: key[j] for j, attr in enumerate({phiParams['V']})}}
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
    """
    
    # Saves the generated program and executes it as a sub-process.
    with open("_generated.py", "w") as f:
        f.write(generatedProgram)
    subprocess.run([sys.executable, "_generated.py"])

if __name__ == "__main__":
    main()