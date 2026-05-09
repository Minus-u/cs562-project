# CS562 Project

this project is awesome

do not forget to update .env to match your user, password, and database


## Team
**Team Name:** CTRL ALT DB  
**Members:** Ryan Raymundo and Ryan Vaseem  

## Project Description
This project is a query processing engine for SQL-like, MF, and EMF-style queries over the `sales` table.

The program reads a query from either a text file or interactive input, parses the query into its main components, generates a Python file, scans the PostgreSQL `sales` table, computes aggregates using an in-memory `mf_struct`, and prints the final result.

## Requirements
Before running the project, install the required Python packages:

test queries using :

**testing with input file/query**
```python
python thegenerator.py emf_query.txt
```

**testing with interactive input**
```python
python thegenerator.py < test_interactive.txt
```