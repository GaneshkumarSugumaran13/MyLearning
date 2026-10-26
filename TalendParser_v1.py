import os
import sys
import re
import subprocess
import xml.etree.ElementTree as ET

# ==============================================================================
# 1. Dependency Auto-Installer
# ==============================================================================
def _ensure_dependencies():
    """Checks for required Python packages and automatically installs them if missing."""
    required_packages = ['pandas', 'openpyxl']
    for pkg in required_packages:
        try:
            __import__(pkg)
        except ImportError:
            print(f"Package '{pkg}' not found. Installing automatically via pip...")
            subprocess.check_call([sys.executable, "-m", "pip", "install", pkg])

_ensure_dependencies()

import pandas as pd  # Import pandas after dependency verification


# ==============================================================================
# 2. Repository Metadata Parser & Registry Engine
# ==============================================================================
class TalendRepositoryMetadataParser:
    """Scans the metadata/ directory in a Talend project to register external schemas."""
    def __init__(self, project_root_dir):
        self.project_root = project_root_dir
        self.repository_schemas = {}  # {repo_key: [columns]}
        self._load_repository_metadata()

    def _load_repository_metadata(self):
        metadata_dir = os.path.join(self.project_root, "metadata")
        if not os.path.exists(metadata_dir):
            return

        for root, _, files in os.walk(metadata_dir):
            for file in files:
                if file.endswith('.item'):
                    file_path = os.path.join(root, file)
                    try:
                        tree = ET.parse(file_path)
                        xml_root = tree.getroot()
                        repo_id = xml_root.attrib.get('id', '')
                        
                        # Inspect tables, queries, or metadata nodes
                        for table in xml_root.findall('.//tables') + xml_root.findall('.//queries') + xml_root.findall('.//metadata'):
                            table_name = table.attrib.get('name', '') or table.attrib.get('label', '')
                            cols = [
                                c.attrib.get('label', '') or c.attrib.get('name', '') 
                                for c in table.findall('.//columns') 
                                if c.attrib.get('label') or c.attrib.get('name')
                            ]
                            
                            if cols:
                                if repo_id:
                                    self.repository_schemas[repo_id] = cols
                                if table_name:
                                    self.repository_schemas[table_name] = cols
                                if repo_id and table_name:
                                    self.repository_schemas[f"{repo_id} - {table_name}"] = cols
                    except Exception:
                        continue

    def get_schema(self, key):
        if not key:
            return []
        if key in self.repository_schemas:
            return self.repository_schemas[key]
        if ' - ' in key:
            table_part = key.split(' - ')[-1]
            if table_part in self.repository_schemas:
                return self.repository_schemas[table_part]
        return []


# Alias for backward compatibility
TalendRepositoryRegistry = TalendRepositoryMetadataParser


# ==============================================================================
# 3. Helper Functions for SQL Expression Conversion
# ==============================================================================
def convert_java_expr_to_sql(expr):
    """Converts Java/Talend expressions into standard SQL syntax."""
    if not expr or not expr.strip():
        return ""
    
    s = expr.strip()
    
    # Clean string quotes & XML escapes
    s = re.sub(r'&quot;', "'", s)
    s = re.sub(r'\\"', "'", s)
    s = re.sub(r'&gt;', '>', s)
    s = re.sub(r'&lt;', '<', s)
    s = re.sub(r'&amp;', '&', s)
    
    # String handling functions
    s = re.sub(r'StringHandling\.UPPERCASE\((.*?)\)', r'UPPER(\1)', s, flags=re.IGNORECASE)
    s = re.sub(r'StringHandling\.LOWERCASE\((.*?)\)', r'LOWER(\1)', s, flags=re.IGNORECASE)
    s = re.sub(r'StringHandling\.TRIM\((.*?)\)', r'TRIM(\1)', s, flags=re.IGNORECASE)
    s = re.sub(r'Relational\.ISNULL\((.*?)\)', r'\1 IS NULL', s, flags=re.IGNORECASE)
    
    # Ternary operators: (cond) ? val1 : val2 -> CASE WHEN cond THEN val1 ELSE val2 END
    ternary_match = re.match(r'^\s*(.*?)\s*\?\s*(.*?)\s*:\s*(.*?)\s*$', s)
    if ternary_match:
        cond, v1, v2 = ternary_match.groups()
        s = f"CASE WHEN {cond} THEN {v1} ELSE {v2} END"
        
    # Standard logical and equality operators
    s = re.sub(r'\s*==\s*', ' = ', s)
    s = re.sub(r'\s*!=\s*', ' <> ', s)
    s = re.sub(r'\s*&&\s*', ' AND ', s)
    s = re.sub(r'\s*\|\|\s*', ' OR ', s)
    s = re.sub(r'\s*\+\s*', ' || ', s)
    
    return s.strip()


# ==============================================================================
# 4. Job Item Column Mapping Parser
# ==============================================================================
class TalendColumnMappingParser:
    def __init__(self, item_file_path, repo_registry):
        self.file_path = item_file_path
        self.job_name = os.path.splitext(os.path.basename(item_file_path))[0]
        self.tree = ET.parse(item_file_path)
        self.root = self.tree.getroot()
        self.repo_registry = repo_registry
        
        self.nodes = {}
        self.connections = []
        self.tmap_mappings = {}
        
        self._parse_nodes()
        self._parse_connections()
        self._parse_tmaps()

    def _get_element_parameter(self, node, param_name, default=""):
        for param in node.findall('elementParameter'):
            if param.attrib.get('name') == param_name:
                return param.attrib.get('value', default)
        return default

    def _parse_nodes(self):
        for node in self.root.findall('.//node'):
            comp_type = node.attrib.get('componentName', '')
            unique_name = self._get_element_parameter(node, 'UNIQUE_NAME')
            
            # Extract internal metadata schemas
            schemas = {}
            for metadata in node.findall('metadata'):
                connector = metadata.attrib.get('connector', 'FLOW')
                cols = [c.attrib.get('name', '') for c in metadata.findall('column') if c.attrib.get('name')]
                if cols:
                    schemas[connector] = cols

            # Extract repository schema pointers
            repo_schema_type = self._get_element_parameter(node, 'SCHEMA:REPOSITORY_SCHEMA_TYPE')
            repo_prop_type = self._get_element_parameter(node, 'PROPERTY:REPOSITORY_PROPERTY_TYPE')
            
            self.nodes[unique_name] = {
                'unique_name': unique_name,
                'component_type': comp_type,
                'schemas': schemas,
                'repo_schema_type': repo_schema_type,
                'repo_prop_type': repo_prop_type,
                'param_col': (
                    self._get_element_parameter(node, 'NORMALIZE_COLUMN') or 
                    self._get_element_parameter(node, 'JSONFIELD')
                )
            }

    def _parse_connections(self):
        for conn in self.root.findall('.//connection'):
            if conn.attrib.get('lineStyle', '') == '0':  # Data Flow
                self.connections.append({
                    'source': conn.attrib.get('source', ''),
                    'target': conn.attrib.get('target', ''),
                    'label': conn.attrib.get('label', '')
                })

    def _parse_tmaps(self):
        for tmap_node in self.root.findall('.//node[@componentName="tMap"]'):
            node_data = tmap_node.find('nodeData')
            if node_data is None:
                continue

            for out_table in node_data.findall('outputTables'):
                out_table_name = out_table.attrib.get('name', '')
                for entry in out_table.findall('mapperTableEntries'):
                    target_col = entry.attrib.get('name', '')
                    raw_expr = entry.attrib.get('expression', '')
                    sql_expr = convert_java_expr_to_sql(raw_expr)
                    
                    src_col = ""
                    match = re.search(r'[a-zA-Z0-9_]+\.([a-zA-Z0-9_]+)', raw_expr)
                    if match:
                        src_col = match.group(1)
                    elif raw_expr and not re.search(r'[\+\-\*/\?:]', raw_expr):
                        src_col = raw_expr

                    if out_table_name not in self.tmap_mappings:
                        self.tmap_mappings[out_table_name] = []

                    self.tmap_mappings[out_table_name].append({
                        'target_col': target_col,
                        'sql_expression': sql_expr or target_col,
                        'src_col': src_col
                    })

    def _get_node_columns(self, node_id, visited=None):
        if visited is None:
            visited = set()
        if node_id in visited:
            return []
        visited.add(node_id)

        node = self.nodes.get(node_id)
        if not node:
            return []

        # 1. Check direct internal schema
        schemas = node.get('schemas', {})
        for conn_key in ['FLOW', 'RESPONSE', 'SCHEMA']:
            if conn_key in schemas and schemas[conn_key]:
                return schemas[conn_key]

        # 2. Check Repository Registry lookup
        for key in [node.get('repo_schema_type'), node.get('repo_prop_type')]:
            repo_cols = self.repo_registry.get_schema(key)
            if repo_cols:
                return repo_cols

        # 3. Check parameter hints
        if node.get('param_col'):
            return [node['param_col']]

        # 4. Trace upstream connections
        for conn in self.connections:
            if conn['target'] == node_id:
                cols = self._get_node_columns(conn['source'], visited)
                if cols:
                    return cols

        return []

    def generate_direct_column_mapping_dataframe(self):
        records = []

        # Process tMap Expressions
        for stream_label, mappings in self.tmap_mappings.items():
            for m in mappings:
                records.append({
                    'Source Columns (Data fields)': m['src_col'] if m['src_col'] else 'N/A (Derived / Expression)',
                    'Target Columns (Data fields)': m['target_col'],
                    'Transformation (SQL format)': m['sql_expression']
                })

        # Process Direct Flow Connections
        for conn in self.connections:
            src_node = self.nodes.get(conn['source'], {})
            if src_node.get('component_type') == 'tMap':
                continue

            src_cols = self._get_node_columns(conn['source'])
            tgt_cols = self._get_node_columns(conn['target'])

            rule = "DIRECT MOVE"
            comp_type = src_node.get('component_type', '')
            if comp_type == 'tNormalize':
                rule = f"UNNEST / NORMALIZE ({src_node.get('param_col', 'item_tags')})"
            elif comp_type == 'tExtractJSONFields':
                rule = "JSON_TABLE / PARSE JSON"
            elif comp_type == 'tRESTClient':
                rule = "HTTP GET / REST RESPONSE"

            if not src_cols and not tgt_cols:
                stream_col = conn['label'] or 'ALL_COLUMNS (*)'
                records.append({
                    'Source Columns (Data fields)': stream_col,
                    'Target Columns (Data fields)': stream_col,
                    'Transformation (SQL format)': rule
                })
            else:
                if not tgt_cols:
                    tgt_cols = src_cols
                max_len = max(len(src_cols), len(tgt_cols))
                for i in range(max_len):
                    src_c = src_cols[i] if i < len(src_cols) else 'N/A'
                    tgt_c = tgt_cols[i] if i < len(tgt_cols) else src_c
                    records.append({
                        'Source Columns (Data fields)': src_c,
                        'Target Columns (Data fields)': tgt_c,
                        'Transformation (SQL format)': rule
                    })

        df = pd.DataFrame(records)
        return df.drop_duplicates().reset_index(drop=True) if not df.empty else df


# ==============================================================================
# 5. Project Directory Orchestrator
# ==============================================================================
def parse_retail_project(project_root_directory, output_excel_path):
    output_dir = os.path.dirname(os.path.abspath(output_excel_path))
    if output_dir and not os.path.exists(output_dir):
        os.makedirs(output_dir, exist_ok=True)

    repo_registry = TalendRepositoryMetadataParser(project_root_directory)
    process_dir = os.path.join(project_root_directory, "process")
    
    if not os.path.exists(process_dir):
        process_dir = project_root_directory

    item_files = [f for f in os.listdir(process_dir) if f.endswith('.item')]
    if not item_files:
        print(f"No .item files found in: {process_dir}")
        return

    writer = pd.ExcelWriter(output_excel_path, engine='openpyxl')
    parsed_jobs = set()

    print(f"Found {len(item_files)} .item file(s) to process...\n")

    for file_name in item_files:
        full_path = os.path.join(process_dir, file_name)
        try:
            parser = TalendColumnMappingParser(full_path, repo_registry)
            sheet_name = parser.job_name[:31]
            
            counter = 1
            orig_name = sheet_name
            while sheet_name in parsed_jobs:
                sheet_name = f"{orig_name[:28]}_{counter}"
                counter += 1

            df = parser.generate_direct_column_mapping_dataframe()
            df.to_excel(writer, sheet_name=sheet_name, index=False)
            parsed_jobs.add(sheet_name)
            print(f"Parsed sheet [{sheet_name}] ({len(df)} mapped fields)")
        except Exception as e:
            print(f"Error parsing {file_name}: {e}")

    writer.close()
    print(f"\nCompleted! STTM Workbook saved to: {output_excel_path}")


# ==============================================================================
# 6. CLI Execution Entry Point
# ==============================================================================
if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python TalendParser_v1.py <project_root_directory> <output_excel_path>")
        sys.exit(1)

    parse_retail_project(sys.argv[1], sys.argv[2])