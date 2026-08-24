#!/usr/bin/env python3
"""
generate_match_rules.py — Add, update, or delete rules in match-rules.tf based on input CSV.

Usage:
    python scripts/generate_match_rules.py --operation add --input rules/simple_rules.csv --target match-rules.tf
    python scripts/generate_match_rules.py --operation update --input rules/simple_rules.csv --target match-rules.tf
    python scripts/generate_match_rules.py --operation delete --input rules/simple_rules.csv --target match-rules.tf
"""

import argparse
import csv
import sys
from pathlib import Path
import re

# --- object_match_value mini-DSL ---
# CSV column `object_match_value` syntax:
#   ""                              → no objectMatchValue block (use flat match_value)
#   "simple:V1|V2|V3"               → type=simple, value=[V1,V2,V3]
#   "object:NAME:V1|V2"             → type=object, name=NAME, options{value=[V1,V2]}
#
# Returns dict {'type': 'simple'|'object', 'name': str|None, 'values': [str, ...]}
# or None when the column is empty.
def parse_omv(raw: str):
    raw = (raw or '').strip()
    if not raw:
        return None
    if ':' not in raw:
        raise ValueError(f"object_match_value '{raw}' missing kind prefix (expected 'simple:...' or 'object:NAME:...')")
    kind, rest = raw.split(':', 1)
    kind = kind.strip().lower()
    if kind == 'simple':
        return {'type': 'simple', 'name': None, 'values': [v for v in rest.split('|') if v != '']}
    if kind == 'object':
        if ':' not in rest:
            raise ValueError(f"object_match_value '{raw}' object form requires 'object:NAME:V1|V2'")
        name, vals = rest.split(':', 1)
        return {'type': 'object', 'name': name.strip(), 'values': [v for v in vals.split('|') if v != '']}
    raise ValueError(f"object_match_value '{raw}' unknown kind '{kind}' (must be 'simple' or 'object')")

def omv_to_csv(omv) -> str:
    if not omv:
        return ''
    vals = '|'.join(omv.get('values') or [])
    if omv['type'] == 'simple':
        return f'simple:{vals}'
    return f"object:{omv.get('name','')}:{vals}"

# --- Data Model ---
class Rule:
    def __init__(self, name, match_url='', redirect_url='', status_code=301, use_relative_url='none',
                 use_incoming_query_string=False, disabled=False, start=0, end=0, matches_always=False):
        self.name = name
        self.match_url = match_url
        self.redirect_url = redirect_url
        self.status_code = status_code
        self.use_relative_url = use_relative_url
        self.use_incoming_query_string = use_incoming_query_string
        self.disabled = disabled
        self.start = start
        self.end = end
        self.matches_always = matches_always
        self.matches = []  # List of dictionaries holding advanced criteria blocks

    def key(self):
        if self.match_url:
            match_sig = ('simple', self.match_url)
        elif self.matches_always:
            match_sig = ('catch_all',)
        else:
            match_sig = ('complex', tuple(
                (
                    m['match_type'],
                    m.get('match_value', ''),
                    m['match_operator'],
                    omv_to_csv(m.get('object_match_value')),
                )
                for m in self.matches
            ))
        return (self.name, match_sig)

    def to_hcl(self):
        lines = ["  match_rules {"]
        lines.append(f'    name                      = "{self.name}"')
        lines.append(f'    start                     = {self.start}')
        lines.append(f'    end                       = {self.end}')
        if self.matches_always:
            lines.append(f'    matches_always            = true')
        lines.append(f'    use_relative_url          = "{self.use_relative_url}"')
        lines.append(f'    status_code               = {self.status_code}')
        lines.append(f'    redirect_url              = "{self.redirect_url}"')
        lines.append(f'    match_url                 = "{self.match_url}"')
        lines.append(f'    use_incoming_query_string = {"true" if self.use_incoming_query_string else "false"}')
        lines.append(f'    disabled                  = {"true" if self.disabled else "false"}')

        for m in self.matches:
            omv = m.get('object_match_value')
            lines.append("    matches {")
            lines.append(f'      match_type     = "{m["match_type"]}"')
            if not omv:
                lines.append(f'      match_value    = "{m.get("match_value", "")}"')
            lines.append(f'      match_operator = "{m["match_operator"]}"')
            lines.append(f'      case_sensitive = {"true" if m["case_sensitive"] else "false"}')
            lines.append(f'      negate         = {"true" if m["negate"] else "false"}')
            lines.append(f'      check_ips      = "{m["check_ips"]}"')
            if omv:
                quoted = ", ".join(f'"{v}"' for v in (omv.get('values') or []))
                lines.append("      object_match_value {")
                lines.append(f'        type  = "{omv["type"]}"')
                if omv['type'] == 'object':
                    lines.append(f'        name  = "{omv.get("name","")}"')
                    lines.append("        options {")
                    lines.append(f'          value = [{quoted}]')
                    lines.append("        }")
                else:
                    lines.append(f'        value = [{quoted}]')
                lines.append("      }")
            lines.append("    }")

        lines.append("  }")
        return '\n'.join(lines)

# --- Parse CSV ---
def parse_csv(path):
    rules = []
    current_rule = None
    
    with open(path, newline='', encoding='utf-8-sig') as f:
        cleaned_rows = (row for row in f if not row.lstrip().startswith('#'))
        reader = csv.DictReader(cleaned_rows)
        
        for row in reader:
            row = {k.strip(): v.strip() for k, v in row.items() if k}
            
            name = row.get('name', '')
            redirect_url = row.get('redirect_url', '')
            match_url = row.get('match_url', '')
            match_type = row.get('match_type', '')
            
            is_new = (
                current_rule is None or 
                current_rule.name != name or 
                current_rule.redirect_url != redirect_url or 
                match_url != '' or 
                current_rule.match_url != ''
            )
            
            if is_new:
                if current_rule:
                    rules.append(current_rule)
                
                current_rule = Rule(
                    name=name,
                    match_url=match_url,
                    redirect_url=redirect_url,
                    status_code=int(row.get('status_code', '301') or 301),
                    use_relative_url=row.get('use_relative_url', 'none'),
                    use_incoming_query_string=row.get('use_incoming_query_string', 'false').lower() == 'true',
                    disabled=row.get('disabled', 'false').lower() == 'true',
                    start=int(row.get('start', '0') or 0),
                    end=int(row.get('end', '0') or 0),
                    matches_always=row.get('matches_always', 'false').lower() == 'true'
                )
            
            if match_type:
                try:
                    omv = parse_omv(row.get('object_match_value', ''))
                except ValueError as e:
                    print(f"WARNING: skipping match on row for rule '{name}': {e}")
                    omv = None
                match_entry = {
                    'match_type': match_type,
                    'match_value': row.get('match_value', ''),
                    'match_operator': row.get('match_operator', 'equals'),
                    'case_sensitive': row.get('case_sensitive', 'false').lower() == 'true',
                    'negate': row.get('negate', 'false').lower() == 'true',
                    'check_ips': row.get('check_ips', ''),
                }
                if omv:
                    match_entry['object_match_value'] = omv
                    # match_value is mutually exclusive with object_match_value at the API level.
                    match_entry['match_value'] = ''
                current_rule.matches.append(match_entry)
                
        if current_rule:
            rules.append(current_rule)
    return rules

# --- Parse match-rules.tf ---
def extract_blocks(content, keyword):
    blocks = []
    pattern = re.compile(r"\b" + re.escape(keyword) + r"\s*\{")
    i = 0
    while i < len(content):
        m = pattern.search(content, i)
        if not m:
            break
        start = m.end()
        depth = 1
        j = start
        while j < len(content) and depth > 0:
            if content[j] == '{':
                depth += 1
            elif content[j] == '}':
                depth -= 1
            j += 1
        blocks.append(content[m.start():j])
        i = m.start() + 1
    return blocks

def parse_tf(path):
    if not Path(path).exists():
        return []
        
    content = Path(path).read_text(encoding='utf-8')
    blocks = extract_blocks(content, 'match_rules')
    rules = []
    
    for block in blocks:
        def hcl_str(pat, text, default=''):
            m = re.search(pat, text)
            return m.group(1).strip().strip('"') if m else default
        def hcl_bool(pat, text, default=False):
            m = re.search(pat, text)
            return m.group(1).lower() == 'true' if m else default

        name = hcl_str(r'\bname\s*=\s*"([^"]*)"', block)
        match_url = hcl_str(r'\bmatch_url\s*=\s*"([^"]*)"', block)
        redirect_url = hcl_str(r'\bredirect_url\s*=\s*"([^"]*)"', block)
        status_code = int(hcl_str(r'\bstatus_code\s*=\s*(\d+)', block, '301'))
        use_relative_url = hcl_str(r'\buse_relative_url\s*=\s*"([^"]*)"', block, 'none')
        use_incoming_query_string = hcl_bool(r'\buse_incoming_query_string\s*=\s*(true|false)', block)
        disabled = hcl_bool(r'\bdisabled\s*=\s*(true|false)', block)
        start = int(hcl_str(r'\bstart\s*=\s*(\d+)', block, '0'))
        end = int(hcl_str(r'\bend\s*=\s*(\d+)', block, '0'))
        matches_always = hcl_bool(r'\bmatches_always\s*=\s*(true|false)', block)

        rule = Rule(
            name=name, match_url=match_url, redirect_url=redirect_url,
            status_code=status_code, use_relative_url=use_relative_url,
            use_incoming_query_string=use_incoming_query_string, disabled=disabled,
            start=start, end=end, matches_always=matches_always
        )

        matches_blocks = extract_blocks(block, 'matches')
        for mb in matches_blocks:
            entry = {
                'match_type': hcl_str(r'\bmatch_type\s*=\s*"([^"]*)"', mb),
                'match_value': hcl_str(r'\bmatch_value\s*=\s*"([^"]*)"', mb),
                'match_operator': hcl_str(r'\bmatch_operator\s*=\s*"([^"]*)"', mb, 'equals'),
                'case_sensitive': hcl_bool(r'\bcase_sensitive\s*=\s*(true|false)', mb),
                'negate': hcl_bool(r'\bnegate\s*=\s*(true|false)', mb),
                'check_ips': hcl_str(r'\bcheck_ips\s*=\s*"([^"]*)"', mb)
            }
            omv_blocks = extract_blocks(mb, 'object_match_value')
            if omv_blocks:
                ob = omv_blocks[0]
                omv_type = hcl_str(r'\btype\s*=\s*"([^"]*)"', ob, 'simple')
                omv_name = hcl_str(r'\bname\s*=\s*"([^"]*)"', ob, '')
                m_vals = re.search(r'\bvalue\s*=\s*\[([^\]]*)\]', ob)
                values = []
                if m_vals:
                    values = [v.strip().strip('"') for v in m_vals.group(1).split(',') if v.strip()]
                entry['object_match_value'] = {
                    'type': omv_type,
                    'name': omv_name or None,
                    'values': values,
                }
                entry['match_value'] = ''
            rule.matches.append(entry)
        rules.append(rule)
    return rules

def write_tf(path, rules, header=None):
    lines = []
    if header:
        lines.append(header.strip() + '\n')
    lines.append('data "akamai_cloudlets_edge_redirector_match_rule" "match_rules_er" {')
    for rule in rules:
        lines.append(rule.to_hcl())
    lines.append('}')
    Path(path).write_text('\n'.join(lines) + '\n', encoding='utf-8')

# --- Main Logic ---
def main():
    parser = argparse.ArgumentParser(description='Modify match-rules.tf based on input CSV and operation.')
    parser.add_argument('--operation', required=True, help='Operation: add, update, or delete')
    parser.add_argument('--input', required=True, help='Input CSV file')
    parser.add_argument('--target', required=True, help='Target match-rules.tf file')
    args = parser.parse_args()

    op = args.operation.strip().lower()
    if op not in {'add', 'update', 'delete'}:
        print(f"Error: Unknown operation '{args.operation}'. Choose add, update, or delete.")
        sys.exit(1)

    input_rules = parse_csv(args.input)
    tf_rules = parse_tf(args.target)

    input_keys = {r.key(): r for r in input_rules}
    tf_keys = {r.key(): i for i, r in enumerate(tf_rules)}

    if op == 'add':
        new_rules_to_prepend = []
        for k, r in input_keys.items():
            if k not in tf_keys:
                new_rules_to_prepend.append(r)
            else:
                print(f"Skipping add: Rule '{k[0]}' with this match criteria already exists.")
        # Prepend new rules to the top of the existing list
        tf_rules = new_rules_to_prepend + tf_rules
        
    elif op == 'update':
        for k, r in input_keys.items():
            if k in tf_keys:
                tf_rules[tf_keys[k]] = r
            else:
                print(f"Skipping update: Rule '{k[0]}' matching criteria was not found.")
                
    elif op == 'delete':
        tf_rules = [r for r in tf_rules if r.key() not in input_keys]

    header = None
    if Path(args.target).exists():
        with open(args.target, encoding='utf-8') as f:
            for line in f:
                if line.strip().startswith('data '):
                    break
                if line.strip():
                    header = (header or '') + line

    write_tf(args.target, tf_rules, header)
    print(f"Operation '{op}' executed successfully on {args.target}.")

if __name__ == '__main__':
    main()