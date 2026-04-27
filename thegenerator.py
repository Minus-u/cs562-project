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
    phiParams['GROUPING_VARIABLES'] = []
    
    having = input('# 6. HAVING (or NONE): \n')
    phiParams['HAVING'] = None if having.upper() == 'NONE' else having
    return phiParams

def parseInput(data):
    # Parses text file for the ESQL query
    data = " ".join(data.split())
    phiParams = {}

    try:
        # 1. Extract S (Select Attributes)
        selectClause = re.search(r'SELECT (.*?) FROM', data, re.IGNORECASE)
        phiParams['S'] = [val.strip() for val in selectClause.group(1).split(',')] if selectClause else []

        # 2. Extract V and n (Grouping Attributes and Count)
        groupByClause = re.search(
            r'GROUP\s+BY\s+(.*?)(?:\s+SUCH\s+THAT|\s+HAVING|\s+ORDER\s+BY|$)',
            data,
            re.IGNORECASE
        )
        if groupByClause:
            groupText = groupByClause.group(1).strip().rstrip(';')

            if ';' in groupText:
                # MF/EMF format: GROUP BY cust; x, y, z
                groupSections = groupText.split(';', 1)

                phiParams['V'] = []
                for val in groupSections[0].split(','):
                    value = val.strip()
                    if value:
                        phiParams['V'].append(value)

                phiParams['GROUPING_VARIABLES'] = []
                for val in groupSections[1].split(','):
                    value = val.strip()
                    if value:
                        phiParams['GROUPING_VARIABLES'].append(value)

                phiParams['n'] = len(phiParams['GROUPING_VARIABLES'])
            else:
                # Simple SQL format: GROUP BY cust, prod
                phiParams['V'] = []
                for val in groupText.split(','):
                    value = val.strip().rstrip(';')
                    if value:
                        phiParams['V'].append(value)

                phiParams['GROUPING_VARIABLES'] = []
                phiParams['n'] = 0
        else:
            phiParams['V'] = []
            phiParams['GROUPING_VARIABLES'] = []
            phiParams['n'] = 0

        # 3. Extract F-VECT (Aggregate Functions)
        phiParams['F-VECT'] = [item for item in phiParams['S'] if '(' in item]

        # 4. Extract PRED-LIST (Predicates / Sigma)
        suchThatClause = re.search(
            r'SUCH\s+THAT\s+(.*?)(?:\s+HAVING|\s+ORDER\s+BY|$)',
            data,
            re.IGNORECASE
        )
        if suchThatClause:
            predicateText = suchThatClause.group(1).strip().rstrip(';')
            phiParams['PRED-LIST'] = []
            for val in re.split(r'\s*[,;]\s*', predicateText):
                value = val.strip()
                if value:
                    phiParams['PRED-LIST'].append(value)
        else:
            phiParams['PRED-LIST'] = []

        # 4.5 Extract simple WHERE condition for non-EMF grouped query path
        whereClause = re.search(r'WHERE (.*?)( GROUP BY| ORDER BY| HAVING|$)', data, re.IGNORECASE)
        if whereClause:
            phiParams['WHERE'] = whereClause.group(1).strip()
        else:
            phiParams['WHERE'] = None

        # 4.6 Extract ORDER BY for simple grouped query path
        orderByClause = re.search(r'ORDER BY (.*?)( HAVING|$)', data, re.IGNORECASE)
        if orderByClause:
            phiParams['ORDER BY'] = [val.strip().rstrip(';') for val in orderByClause.group(1).split(',')]
        else:
            phiParams['ORDER BY'] = None

        # 5. Extract HAVING (G)
        havingClause = re.search(r'HAVING (.*)', data, re.IGNORECASE)
        if havingClause:
            val = havingClause.group(1).strip()
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

def makeAggregateName(expr):
    return expr.replace("(", "_").replace(")", "").replace(".", "_").replace("*", "star").replace(" ", "")

def main():
    phiParams = getUserInput()

    # Prepare aggregate initialization values
    initialAggregates = {}

    for agg in phiParams['F-VECT']:
        aggregateName = makeAggregateName(agg)

        if agg.lower().startswith("avg("):
            initialAggregates[aggregateName + "__sum"] = 0
            initialAggregates[aggregateName + "__count"] = 0
        else:
            initialAggregates[aggregateName] = 0
    groupCols = phiParams['V'] if len(phiParams['V']) > 0 else ['cust', 'prod']

    # Parse simple aggregate expressions in SELECT, for example avg(quant), max(quant), count(*)
    aggregates = []
    for agg_expr in phiParams['F-VECT']:
        aggregateCall = re.match(r"^\s*([A-Za-z_]\w*)\s*\(\s*([A-Za-z_][\w\.]*|\*)\s*\)\s*$", agg_expr)
        if aggregateCall:
            aggFunc = aggregateCall.group(1).lower()
            aggCol = aggregateCall.group(2)
            if aggFunc in ["avg", "max", "min", "sum", "count"]:
                aggregates.append({
                    "expr": agg_expr,
                    "func": aggFunc,
                    "col": aggCol
                })

    # Parse basic WHERE predicates joined by AND
    filters = []
    if phiParams.get('WHERE'):
        filterTextList = re.split(r"\s+and\s+", phiParams['WHERE'], flags=re.IGNORECASE)
        for part in filterTextList:
            parsedFilter = re.match(r"^\s*([A-Za-z_]\w*)\s*(=|!=|>=|<=|>|<)\s*('?[^']*'?|\d+)\s*$", part.strip())
            if parsedFilter:
                leftCol = parsedFilter.group(1)
                operator = parsedFilter.group(2)
                rawFilterValue = parsedFilter.group(3).strip()
                if rawFilterValue.startswith("'") and rawFilterValue.endswith("'"):
                    rightVal = rawFilterValue[1:-1]
                    rightType = "str"
                else:
                    rightVal = int(rawFilterValue)
                    rightType = "num"
                filters.append({
                    "left": leftCol,
                    "op": operator,
                    "right": rightVal,
                    "right_type": rightType
                })

    # This simple query path is enabled only when input is not using SUCH THAT
    simpleMode = len([p for p in phiParams['PRED-LIST'] if p]) == 0 and len(groupCols) > 0 and len(aggregates) > 0
    mfMode = phiParams['n'] > 0 and len(phiParams['PRED-LIST']) > 0
    
    firstScanCode = f"""
    cur.execute("SELECT * FROM sales")
    for row in cur:
        # Create a unique key for the group based on attributes V: {phiParams['V']}
        key = tuple(row[attr] for attr in {phiParams['V']})
        if key not in mf_struct:
            # Initialize aggregate variables: {phiParams['F-VECT']}
            mf_struct[key] = {initialAggregates}
    """

    # Build generated code blocks for aggregate initialization, updates, and output values
    aggregateSetupLines = []
    aggregateUpdateLines = []
    aggregateOutputLines = []

    for spec in aggregates:
        expr = spec["expr"]
        func = spec["func"]
        col = spec["col"]
        aggregateName = makeAggregateName(expr)

        if func == "sum":
            aggregateSetupLines.append(f"                    '{aggregateName}': 0,")
            if col == "*":
                aggregateUpdateLines.append(f"            grouped_results[groupKey]['{aggregateName}'] = grouped_results[groupKey]['{aggregateName}'] + 1")
            else:
                aggregateUpdateLines.append(f"            grouped_results[groupKey]['{aggregateName}'] = grouped_results[groupKey]['{aggregateName}'] + row['{col}']")
            aggregateOutputLines.append(f"            outputRow['{expr}'] = groupData['{aggregateName}']")

        elif func == "count":
            aggregateSetupLines.append(f"                    '{aggregateName}': 0,")
            aggregateUpdateLines.append(f"            grouped_results[groupKey]['{aggregateName}'] = grouped_results[groupKey]['{aggregateName}'] + 1")
            aggregateOutputLines.append(f"            outputRow['{expr}'] = groupData['{aggregateName}']")

        elif func == "max":
            if col == "*":
                aggregateSetupLines.append(f"                    '{aggregateName}': None,")
                aggregateUpdateLines.append(f"            if grouped_results[groupKey]['{aggregateName}'] is None or 1 > grouped_results[groupKey]['{aggregateName}']:")
                aggregateUpdateLines.append(f"                grouped_results[groupKey]['{aggregateName}'] = 1")
            else:
                aggregateSetupLines.append(f"                    '{aggregateName}': None,")
                aggregateUpdateLines.append(f"            if grouped_results[groupKey]['{aggregateName}'] is None or row['{col}'] > grouped_results[groupKey]['{aggregateName}']:")
                aggregateUpdateLines.append(f"                grouped_results[groupKey]['{aggregateName}'] = row['{col}']")
            aggregateOutputLines.append(f"            outputRow['{expr}'] = groupData['{aggregateName}']")

        elif func == "min":
            if col == "*":
                aggregateSetupLines.append(f"                    '{aggregateName}': None,")
                aggregateUpdateLines.append(f"            if grouped_results[groupKey]['{aggregateName}'] is None or 1 < grouped_results[groupKey]['{aggregateName}']:")
                aggregateUpdateLines.append(f"                grouped_results[groupKey]['{aggregateName}'] = 1")
            else:
                aggregateSetupLines.append(f"                    '{aggregateName}': None,")
                aggregateUpdateLines.append(f"            if grouped_results[groupKey]['{aggregateName}'] is None or row['{col}'] < grouped_results[groupKey]['{aggregateName}']:")
                aggregateUpdateLines.append(f"                grouped_results[groupKey]['{aggregateName}'] = row['{col}']")
            aggregateOutputLines.append(f"            outputRow['{expr}'] = groupData['{aggregateName}']")

        elif func == "avg":
            avgSumName = aggregateName + "__sum"
            avgCountName = aggregateName + "__count"
            aggregateSetupLines.append(f"                    '{avgSumName}': 0,")
            aggregateSetupLines.append(f"                    '{avgCountName}': 0,")
            if col == "*":
                aggregateUpdateLines.append(f"            grouped_results[groupKey]['{avgSumName}'] = grouped_results[groupKey]['{avgSumName}'] + 1")
            else:
                aggregateUpdateLines.append(f"            grouped_results[groupKey]['{avgSumName}'] = grouped_results[groupKey]['{avgSumName}'] + row['{col}']")
            aggregateUpdateLines.append(f"            grouped_results[groupKey]['{avgCountName}'] = grouped_results[groupKey]['{avgCountName}'] + 1")
            aggregateOutputLines.append(f"            if groupData['{avgCountName}'] > 0:")
            aggregateOutputLines.append(f"                outputRow['{expr}'] = groupData['{avgSumName}'] / groupData['{avgCountName}']")
            aggregateOutputLines.append("            else:")
            aggregateOutputLines.append(f"                outputRow['{expr}'] = 0")

    filterLines = ["        should_use_row = True"]
    for cond in filters:
        op = "==" if cond["op"] == "=" else cond["op"]
        if cond["right_type"] == "str":
            filterLines.append(f"        if not (row['{cond['left']}'] {op} '{cond['right']}'):")
            filterLines.append("            should_use_row = False")
        else:
            filterLines.append(f"        if not (row['{cond['left']}'] {op} {cond['right']}):")
            filterLines.append("            should_use_row = False")

    sortCols = phiParams['ORDER BY'] if phiParams.get('ORDER BY') else groupCols

    groupedQueryCode = f"""
    grouped_results = {{}}

    cur.execute("SELECT * FROM sales")
    for row in cur:
{chr(10).join(filterLines)}
        # Use current row when it matches the parsed filter condition
        if should_use_row:
            # Build the group key using parsed grouping attributes
            groupKey = tuple(row[attr] for attr in {groupCols})

            # Create a new group entry the first time we see this key
            if groupKey not in grouped_results:
                grouped_results[groupKey] = {{
{chr(10).join(aggregateSetupLines)}
                }}

{chr(10).join(aggregateUpdateLines)}
    """
    if not simpleMode:
        groupedQueryCode = "grouped_results = {}"


    groupingVarCode = ""
    mfWarning = ""

    # Keep single-variable mode explicit to avoid partial multi-variable behavior
    if mfMode and phiParams['n'] != 1:
        mfWarning = f"print('Current MF mode supports exactly one grouping variable. Input has n={phiParams['n']}.')"
    elif mfMode and len(phiParams['GROUPING_VARIABLES']) > 0 and len(phiParams['PRED-LIST']) > 0:
        gv = phiParams['GROUPING_VARIABLES'][0]
        pred = phiParams['PRED-LIST'][0]

        predicateChecks = []
        predicateChecks.append("        rowMatchesPredicate = True")
        predicateConditions = re.split(r"\s+and\s+", pred, flags=re.IGNORECASE)

        for conditionText in predicateConditions:
            conditionText = conditionText.strip()
            parsedFilter = re.match(
                rf"{gv}[.]([A-Za-z_]\w*)\s*(=|!=|>=|<=|>|<)\s*('([^']*)'|\d+)",
                conditionText
            )
            if parsedFilter:
                condCol = parsedFilter.group(1)
                condOp = parsedFilter.group(2)
                rawFilterValue = parsedFilter.group(3)
                conditionValue = rawFilterValue.strip("'")

                if rawFilterValue.startswith("'") and rawFilterValue.endswith("'"):
                    conditionCode = f"row['{condCol}'] {condOp if condOp != '=' else '=='} '{conditionValue}'"
                else:
                    conditionCode = f"row['{condCol}'] {condOp if condOp != '=' else '=='} {conditionValue}"

                predicateChecks.append(f"        if not ({conditionCode}):")
                predicateChecks.append("            rowMatchesPredicate = False")

        aggregateUpdateCode = []

        for spec in aggregates:
            expr = spec["expr"]
            func = spec["func"]
            col = spec["col"]

            aggregateName = makeAggregateName(expr)

            if "." in col:
                colParts = col.split(".")
                groupingVar = colParts[0]
                sourceColumn = colParts[1]
            else:
                groupingVar = gv
                sourceColumn = col

            if groupingVar == gv:
                if func == "sum":
                    aggregateUpdateCode.append(f"                mf_struct[groupKeyValues]['{aggregateName}'] += row['{sourceColumn}']")
                elif func == "count":
                    aggregateUpdateCode.append(f"                mf_struct[groupKeyValues]['{aggregateName}'] += 1")
                elif func == "avg":
                    avgSum = aggregateName + "__sum"
                    avgCount = aggregateName + "__count"
                    aggregateUpdateCode.append(f"                mf_struct[groupKeyValues]['{avgSum}'] += row['{sourceColumn}']")
                    aggregateUpdateCode.append(f"                mf_struct[groupKeyValues]['{avgCount}'] += 1")
                elif func == "max":
                    aggregateUpdateCode.append(f"                if mf_struct[groupKeyValues]['{aggregateName}'] == 0 or row['{sourceColumn}'] > mf_struct[groupKeyValues]['{aggregateName}']:")
                    aggregateUpdateCode.append(f"                    mf_struct[groupKeyValues]['{aggregateName}'] = row['{sourceColumn}']")
                elif func == "min":
                    aggregateUpdateCode.append(f"                if mf_struct[groupKeyValues]['{aggregateName}'] == 0 or row['{sourceColumn}'] < mf_struct[groupKeyValues]['{aggregateName}']:")
                    aggregateUpdateCode.append(f"                    mf_struct[groupKeyValues]['{aggregateName}'] = row['{sourceColumn}']")

        if len(aggregateUpdateCode) == 0:
            aggregateUpdateCode.append("                pass")

        groupingVarCode = f"""
    # --- MF SCAN FOR GROUPING VARIABLE {gv} ---
    cur.execute("SELECT * FROM sales")
    for row in cur:
{chr(10).join(predicateChecks)}
        if rowMatchesPredicate:
            groupKeyValues = tuple(row[attr] for attr in {phiParams['V']})
            if groupKeyValues in mf_struct:
{chr(10).join(aggregateUpdateCode)}
"""

    generatedProgram = f"""
import os
import re
import psycopg2
import psycopg2.extras
import tabulate
from dotenv import load_dotenv

# DO NOT EDIT THIS FILE, IT IS GENERATED BY generator.py

def query():
    def makeAggName(expr):
        return expr.replace("(", "_").replace(")", "").replace(".", "_").replace("*", "star").replace(" ", "")

    def passesHaving(row):
        havingExpr = {repr(phiParams['HAVING'])}
        if havingExpr is None:
            return True

        evalExpr = havingExpr
        evalExpr = re.sub(r"\\bAND\\b", " and ", evalExpr, flags=re.IGNORECASE)
        evalExpr = re.sub(r"\\bOR\\b", " or ", evalExpr, flags=re.IGNORECASE)
        evalExpr = re.sub(r"(?<![<>=!])=(?!=)", "==", evalExpr)

        aggCalls = re.findall(r"[A-Za-z_]\\w*\\s*\\(\\s*(?:[A-Za-z_][\\w\\.]*|\\*)\\s*\\)", evalExpr)
        for aggExpr in sorted(set(aggCalls), key=len, reverse=True):
            evalExpr = evalExpr.replace(aggExpr, makeAggName(aggExpr))

        evalVals = {{}}
        for key, val in row.items():
            evalVals[makeAggName(str(key))] = val
            if re.match(r"^[A-Za-z_]\\w*$", str(key)):
                evalVals[str(key)] = val

        try:
            return bool(eval(evalExpr, {{"__builtins__": {{}}}}, evalVals))
        except Exception:
            return False

    load_dotenv()
    # ... (connection setup) ...
    conn = psycopg2.connect(dbname=os.getenv('DBNAME'), 
                            user=os.getenv('USER'), 
                            password=os.getenv('PASSWORD'),
                            cursor_factory=psycopg2.extras.DictCursor)
    cur = conn.cursor()
    
    mf_struct = {{}}
    
    # --- TABLE SCAN 1: populate mf-struct with distinct values of grouping attribute (V) ---
    {firstScanCode}
    
    # --- TABLE SCAN 2: simple grouped processing ---
    {groupedQueryCode}
    
    # --- MF/EMF grouping variable scan ---
    {groupingVarCode}
    {mfWarning}

    output = []
    if {simpleMode}:
        for groupKey in grouped_results:
            groupData = grouped_results[groupKey]

            outputRow = {{}}
            for i, attrName in enumerate({groupCols}):
                outputRow[attrName] = groupKey[i]

{chr(10).join(aggregateOutputLines)}
            if passesHaving(outputRow):
                output.append(outputRow)

        # Sort grouped output so generated result is stable for testing
        output.sort(key=lambda outRow: tuple(outRow[attrName] for attrName in {sortCols}))
    else:        
        for key, aggs in mf_struct.items():
            row = {{attr: key[i] for i, attr in enumerate({phiParams['V']})}}

            for aggregateName, aggregateValue in aggs.items():
                if aggregateName.endswith("__sum"):
                    averageName = aggregateName[:-5]
                    avgCountName = averageName + "__count"
                    if avgCountName in aggs and aggs[avgCountName] != 0:
                        row[averageName] = aggs[aggregateName] / aggs[avgCountName]
                    else:
                        row[averageName] = 0
                elif aggregateName.endswith("__count") and (aggregateName[:-7] + "__sum") in aggs:
                    continue
                else:
                    row[aggregateName] = aggregateValue

            if passesHaving(row):
                output.append(row)

    return tabulate.tabulate(output, headers="keys", tablefmt="psql")
    

if "__main__" == __name__:
    print(query())
    """

    open("_generated.py", "w").write(generatedProgram)
    subprocess.run([sys.executable, "_generated.py"])

if "__main__" == __name__:
    main()