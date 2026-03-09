"""
Générateur de Tests de Contrat API Ultime
Fusionne l'extraction exhaustive des schémas (V3) et le squelette robuste (V0).
Prérequis: PyYAML installé (pip install pyyaml)
Usage: python generate_test_cases_v0.py --input swagger.json [--verbose]
"""

import json
import os
import yaml
import argparse
import re
import copy
from pathlib import Path

# Mode verbose pour debug
VERBOSE = False

# Structure pour stocker toutes les séquences de tests
sequences = []

# Fichier Swagger/OpenAPI source
DEFAULT_SWAGGER_FILE = "swagger.json"

# Compteurs pour les statistiques
stats = {
    "operations": 0,
    "sequences_created": 0,
    "steps_created": 0
}

def resolve_schema_ref(schema_ref, definitions):
    """Résout une référence de schéma $ref"""
    if not schema_ref or not isinstance(schema_ref, str) or not definitions:
        return None
    schema_name = schema_ref.split("/")[-1]
    resolved = definitions.get(schema_name)
    if VERBOSE:
        print(f"  [VERBOSE] resolve_schema_ref: {schema_ref} -> {schema_name} -> {'found' if resolved else 'NOT FOUND'}")
    return resolved

def extract_exhaustive_schema(schema, definitions, depth=0, max_depth=20):
    """Extrait TOUTES les contraintes de validation d'un schéma de manière exhaustive.
    
    Gère: $ref, allOf/anyOf/oneOf, nullable, string constraints (format, pattern,
    minLength, maxLength, enum), number constraints (minimum, maximum, exclusiveMinimum,
    exclusiveMaximum, multipleOf), array constraints (minItems, maxItems, uniqueItems, items),
    objets imbriqués avec propriétés.
    """
    if not schema or depth > max_depth:
        return None

    # Résoudre les $ref
    if "$ref" in schema:
        resolved = resolve_schema_ref(schema["$ref"], definitions)
        return extract_exhaustive_schema(resolved, definitions, depth + 1, max_depth) if resolved else None

    # Gérer les compositions allOf / anyOf / oneOf
    for composer in ["allOf", "anyOf", "oneOf"]:
        if composer in schema:
            return {
                "composition": composer,
                "sub_schemas": [extract_exhaustive_schema(s, definitions, depth + 1, max_depth) for s in schema[composer]]
            }

    rules = {"type": schema.get("type", "string")}

    # Nullable
    if schema.get("nullable") or schema.get("x-nullable"):
        rules["nullable"] = True

    if rules["type"] == "string":
        for attr in ["format", "pattern", "minLength", "maxLength", "enum"]:
            if attr in schema:
                rules[attr] = schema[attr]
    elif rules["type"] in ["integer", "number"]:
        for attr in ["minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"]:
            if attr in schema:
                rules[attr] = schema[attr]
    elif rules["type"] == "array":
        for attr in ["minItems", "maxItems", "uniqueItems"]:
            if attr in schema:
                rules[attr] = schema[attr]
        if "items" in schema:
            rules["items"] = extract_exhaustive_schema(schema["items"], definitions, depth + 1, max_depth)
    elif rules["type"] == "object" or "properties" in schema:
        rules["type"] = "object"
        rules["required"] = schema.get("required", [])
        properties = schema.get("properties", {})
        if properties:
            rules["properties"] = {}
            for p_name, p_def in properties.items():
                rules["properties"][p_name] = extract_exhaustive_schema(p_def, definitions, depth + 1, max_depth)

    if VERBOSE:
        print(f"  [VERBOSE] extract_exhaustive_schema (depth={depth}): type={rules.get('type')}")

    return rules

def get_body_schema_from_operation(operation, definitions):
    """Extrait le schéma du body proprement (OpenAPI 3.x & Swagger 2.0)"""
    # OpenAPI 3.x: requestBody
    if "requestBody" in operation:
        content = operation["requestBody"].get("content", {})
        for content_type in ["application/json", "application/hal+json", "*/*"]:
            if content_type in content:
                schema_obj = content[content_type].get("schema")
                if schema_obj:
                    if "$ref" in schema_obj:
                        return resolve_schema_ref(schema_obj["$ref"], definitions)
                    return schema_obj
        return None

    # Swagger 2.0: parameters with in=body
    for p in operation.get("parameters", []):
        if p.get("in") == "body" and "schema" in p:
            schema_obj = p["schema"]
            if "$ref" in schema_obj:
                return resolve_schema_ref(schema_obj["$ref"], definitions)
            return schema_obj
    return None

def get_endpoint_required_fields(operation, body_schema):
    """
    Retourne les champs réellement requis pour cet endpoint spécifique.
    Applique la même logique que Swagger UI:
    - Si requestBody.required = true, utilise les required du schéma
    - Si requestBody.required = false, aucun champ du body n'est requis
    """
    if not body_schema:
        return []
    
    # Vérifier si le requestBody lui-même est requis (default = false en OpenAPI 3.x)
    if "requestBody" in operation:
        request_body_required = operation["requestBody"].get("required", False)
        if not request_body_required:
            # Si le requestBody n'est pas requis, aucun champ n'est forcément requis
            return []
    
    # Sinon, utiliser les required du schéma
    return body_schema.get("required", [])

def create_test_step(label, method, path, status_code="200", phase="action",
                     resp_schema_raw=None, body_override=None, query_override=None,
                     remove_auth=False, req_schema_rules=None, q_params=None,
                     definitions=None):
    """Crée un step de test conforme au schéma api-contract avec validation exhaustive (V3)"""
    stats["steps_created"] += 1

    step = {
        "label": label,
        "phase": phase,
        "request": {
            "method": method.upper(),
            "path": path,
            "headers": {"Accept": "application/json"},
            "query": {}
        },
        "expect": {
            "status": int(status_code) if str(status_code).isdigit() else 200
        }
    }
    
    # Ajouter content_type seulement si le code le justifie (pas 204, 304)
    status_int = int(status_code) if str(status_code).isdigit() else 200
    if status_int not in [204, 304]:
        step["expect"]["content_type"] = "application/json"
    
    # Validation exhaustive du contrat (seulement pour les succès)
    if resp_schema_raw and str(status_code).startswith("2"):
        validation_rules = extract_exhaustive_schema(resp_schema_raw, definitions or {})
        if validation_rules:
            step["expect"]["contract_validation"] = validation_rules
            if VERBOSE:
                print(f"  [VERBOSE] create_test_step: Added contract_validation for {label}")

    # Supprimer l'auth pour tests négatifs
    if remove_auth:
        step["request"]["headers"]["Authorization"] = ""
    
    # Query Params
    if query_override is not None:
        step["request"]["query"] = query_override
    elif q_params:
        step["request"]["query"] = {p["name"]: f"${{{p['name']}}}" for p in q_params if p.get("required")}
    
    # Body
    if body_override is not None:
        step["request"]["body"] = json.dumps(body_override) if isinstance(body_override, dict) else body_override
        step["request"]["headers"]["Content-Type"] = "application/json"
    elif req_schema_rules and method.upper() in ["POST", "PUT", "PATCH"]:
        body_obj = generate_smart_example(req_schema_rules)
        step["request"]["body"] = json.dumps(body_obj, indent=2)
        step["request"]["headers"]["Content-Type"] = "application/json"
    
    # Cleanup empty query
    if not step["request"]["query"]:
        del step["request"]["query"]
    
    return step

def generate_smart_example(rules, use_invalid_values=False):
    """Génère une valeur d'exemple à partir des règles exhaustives du schéma.
    Supporte la génération de valeurs valides et invalides (pour tests négatifs)."""
    if not rules:
        return "example"
    
    t = rules.get("type", "string")

    # --- GÉNÉRATION DE VALEURS INVALIDES ---
    if use_invalid_values:
        if rules.get("enum"):
            return "INVALID_ENUM_VALUE_NOT_IN_LIST"
        if t == "string":
            fmt = rules.get("format")
            if fmt == "uuid": return "not-a-uuid"
            if fmt == "date-time": return "invalid-date"
            if fmt == "email": return "not-an-email"
            max_len = rules.get("maxLength", 1000)
            return "x" * (max_len + 10)
        if t in ["integer", "number"]:
            if "maximum" in rules: return rules["maximum"] + 1000
            return -999999
        if t == "boolean": return "not_a_boolean"
        if t == "array": return "not_an_array"
        if t == "object": return "not_an_object"
        return "invalid_value"

    # --- GÉNÉRATION DE VALEURS VALIDES ---
    if rules.get("enum"):
        return rules["enum"][0]

    if t == "string":
        fmt = rules.get("format")
        if fmt == "uuid": return "550e8400-e29b-41d4-a716-446655440000"
        if fmt == "date-time": return "2026-01-09T10:00:00Z"
        if fmt == "email": return "test@example.com"
        return rules.get("pattern", f"test_{rules.get('minLength', '')}")

    if t in ["integer", "number"]:
        return rules.get("minimum", 1)
    if t == "boolean":
        return True
    if t == "array":
        item_ex = generate_smart_example(rules.get("items"))
        return [item_ex] if item_ex else []
    if t == "object":
        obj = {}
        props = rules.get("properties", {})
        for name in rules.get("required", []):
            if name in props:
                obj[name] = generate_smart_example(props[name])
        return obj
    return None

def extract_path_parameters(path):
    """Extrait les paramètres de path {param} et les convertit en ${param}"""
    return re.sub(r'\{([^}]+)\}', r'${\1}', path)

def create_sequence_from_operation(swagger_data, path, method, operation):
    """Crée une séquence de tests complète pour une opération API
    Combine le squelette V0 et la logique exhaustive V3."""
    
    # Générer un operationId à partir du summary ou du path si absent
    operation_id = operation.get("operationId")
    summary = operation.get("summary", "")
    
    if not operation_id:
        if summary:
            operation_id = summary.replace(" ", "_").replace(".", "").replace(",", "").replace("-", "_")[:50]
        else:
            clean_path = path.replace("/", "_").replace("{", "").replace("}", "")
            operation_id = f"{method.upper()}{clean_path}"[:50]
    
    if not summary:
        summary = operation_id
    
    tags = operation.get("tags", ["API"])
    domain_name = tags[0] if tags else "API"
    
    jmeter_path = extract_path_parameters(path)
    
    # Récupérer les définitions (Swagger 2.0 + OpenAPI 3.x)
    definitions = swagger_data.get("definitions", {})
    if not definitions and "components" in swagger_data:
        definitions = swagger_data.get("components", {}).get("schemas", {})
    
    if VERBOSE:
        print(f"[VERBOSE] Operation {operation_id}: Found {len(definitions)} definitions")
    
    # --- Analyse Préliminaire (V3) ---
    q_params = [p for p in operation.get("parameters", []) if p.get("in") == "query"]
    body_schema = get_body_schema_from_operation(operation, definitions)
    req_schema_rules = extract_exhaustive_schema(body_schema, definitions) if body_schema else None
    endpoint_required_body_fields = get_endpoint_required_fields(operation, body_schema)
    
    if VERBOSE:
        print(f"[VERBOSE] {operation_id}: endpoint_required_body_fields = {endpoint_required_body_fields}")
    
    # Extraire le code de succès et le schéma de réponse
    success_code = next((code for code in operation.get("responses", {}) if code.startswith("2")), "200")
    success_resp = operation.get("responses", {}).get(success_code, {})
    s_schema = (success_resp.get("content", {}).get("application/json", {}).get("schema")
                or success_resp.get("schema"))
    
    # Créer la séquence
    sequence = {
        "name": f"{domain_name} - {operation_id}",
        "tags": ["contract", domain_name.split('_')[1] if '_' in domain_name else "api", method.upper()],
        "prereqs": [
            "Authentification requise via token Bearer"
        ],
        "steps": []
    }
    
    # ==========================================
    # 1. SCÉNARIO NOMINAL (Succès)
    # ==========================================
    sequence["steps"].append(create_test_step(
        label=f"{method.upper()} {operation_id} - Nominal Success",
        method=method, path=jmeter_path, status_code=success_code,
        resp_schema_raw=s_schema, req_schema_rules=req_schema_rules,
        q_params=q_params, definitions=definitions
    ))
    
    # ==========================================
    # 2. ERREURS DOCUMENTÉES DANS LE SWAGGER
    # ==========================================
    for code, resp in operation.get("responses", {}).items():
        if code.startswith("2") or code == "default":
            continue
        desc = resp.get("description", f"Status {code}")
        e_schema = (resp.get("content", {}).get("application/json", {}).get("schema")
                    or resp.get("schema"))
        
        sequence["steps"].append(create_test_step(
            label=f"{method.upper()} {operation_id} - {desc} ({code})",
            method=method, path=jmeter_path, status_code=code,
            resp_schema_raw=e_schema,
            req_schema_rules=req_schema_rules if code != "400" else None,
            q_params=q_params if not code.startswith("4") else None,
            remove_auth=(code == "401"),
            definitions=definitions
        ))
    
    # ==========================================
    # 3. TESTS NÉGATIFS : QUERY PARAMS
    # ==========================================
    required_queries = [p for p in q_params if p.get("required")]
    
    # A. Paramètres requis manquants
    for req_q in required_queries:
        bad_query = {p["name"]: f"${{{p['name']}}}" for p in required_queries if p["name"] != req_q["name"]}
        sequence["steps"].append(create_test_step(
            label=f"{method.upper()} {operation_id} - 400 Missing Query Param: {req_q['name']}",
            method=method, path=jmeter_path, status_code="400",
            query_override=bad_query, definitions=definitions
        ))
    
    # B. Enums invalides dans query params
    for q in q_params:
        q_schema = q.get("schema", {})
        if "enum" in q_schema or "enum" in q:
            bad_query = {p["name"]: f"${{{p['name']}}}" for p in required_queries}
            bad_query[q["name"]] = "INVALID_ENUM_VALUE"
            sequence["steps"].append(create_test_step(
                label=f"{method.upper()} {operation_id} - 400 Bad Enum Query: {q['name']}",
                method=method, path=jmeter_path, status_code="400",
                query_override=bad_query, definitions=definitions
            ))
    
    # ==========================================
    # 4. TESTS NÉGATIFS : BODY (Logique V3 exhaustive)
    # ==========================================
    if req_schema_rules and method.upper() in ["POST", "PUT", "PATCH"]:
        required_fields = req_schema_rules.get("required", [])
        properties = req_schema_rules.get("properties", {})
        valid_body = generate_smart_example(req_schema_rules)
        
        # A. Champs requis manquants (limité à 2 pour ne pas surcharger)
        if isinstance(valid_body, dict):
            for req_field in required_fields[:2]:
                bad_body = copy.deepcopy(valid_body)
                bad_body.pop(req_field, None)
                sequence["steps"].append(create_test_step(
                    label=f"{method.upper()} {operation_id} - 400 Missing Body Field: {req_field}",
                    method=method, path=jmeter_path, status_code="400",
                    body_override=bad_body, q_params=q_params, definitions=definitions
                ))
        
        # B. Mauvais Formats / Enums (limité à 3)
        if isinstance(valid_body, dict) and properties:
            test_count = 0
            for prop_name, prop_rules in properties.items():
                if test_count >= 3:
                    break
                if prop_rules and (prop_rules.get("enum") or prop_rules.get("format")):
                    bad_body = copy.deepcopy(valid_body)
                    bad_body[prop_name] = generate_smart_example(prop_rules, use_invalid_values=True)
                    sequence["steps"].append(create_test_step(
                        label=f"{method.upper()} {operation_id} - 400 Bad Format/Enum: {prop_name}",
                        method=method, path=jmeter_path, status_code="400",
                        body_override=bad_body, q_params=q_params, definitions=definitions
                    ))
                    test_count += 1
    
    return sequence

# Parse command line arguments
parser = argparse.ArgumentParser(description="Generate API contract test cases from Swagger/OpenAPI file")
parser.add_argument("--input", "-i", default=DEFAULT_SWAGGER_FILE, 
                    help=f"Path to Swagger/OpenAPI JSON file (default: {DEFAULT_SWAGGER_FILE})")
parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose debug logging")
args = parser.parse_args()
VERBOSE = args.verbose

if VERBOSE:
    print("[VERBOSE] Verbose mode enabled")
    print(f"[VERBOSE] Input file: {args.input}")

# Charger le fichier Swagger/OpenAPI
swagger_file = args.input
if not os.path.isfile(swagger_file):
    print(f"ERROR: File not found: {swagger_file}")
    exit(1)

print(f"Loading Swagger/OpenAPI file: {swagger_file}")

try:
    # Use 'utf-8-sig' to gracefully handle files that start with a BOM
    with open(swagger_file, 'r', encoding='utf-8-sig') as f:
        swagger_data = json.load(f)
except Exception as e:
    print(f"ERROR: Failed to load {swagger_file}: {e}")
    exit(1)

# Vérifier que c'est un fichier Swagger/OpenAPI valide
if "paths" not in swagger_data:
    print(f"ERROR: Invalid Swagger/OpenAPI file - missing 'paths' section")
    exit(1)

# Analyser toutes les opérations
print(f"Processing {len(swagger_data['paths'])} paths...")

for path, methods in swagger_data["paths"].items():
    for method, operation in methods.items():
        if method.upper() not in ["GET", "POST", "PUT", "DELETE", "PATCH"]:
            continue
        
        stats["operations"] += 1
        operation_id = operation.get("operationId", f"{method.upper()} {path}")
        print(f"  - {method.upper()} {path} ({operation_id})")
        
        # Créer une séquence pour cette opération
        sequence = create_sequence_from_operation(swagger_data, path, method, operation)
        sequences.append(sequence)
        stats["sequences_created"] += 1

# Créer la structure complète du contrat API
api_contract = {
    "api_contract": {
        "name": "API Contract Tests (Auto-Generated - Ultimate)",
        "description": "Tests de contrat générés combinant validation sémantique profonde et tests négatifs exhaustifs.",
        "config": {
            "base_url": "${protocol}://${host}${basePath}",
            "auth_token": "${token}",
            "default_headers": {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": "Bearer ${token}"
            },
            "variables": {
                "protocol": "http",
                "host": "y2devwebappweotku7pzzbik.westeurope.cloudapp.azure.com",
                "port": "",
                "basePath": "/Retail_25.0",
                "ALLOW_DESTRUCTIVE": "false"
            }
        },
        "sequences": sequences
    }
}

# Écrire le fichier YAML
output_yaml = "out/api_contract_generated.yml"
# S'assurer que le répertoire de sortie existe
output_dir = os.path.dirname(output_yaml)
if output_dir and not os.path.exists(output_dir):
    try:
        os.makedirs(output_dir, exist_ok=True)
    except Exception as e:
        print(f"WARNING: failed to create output directory {output_dir}: {e}")

with open(output_yaml, "w", encoding="utf-8") as yamlfile:
    yaml.dump(api_contract, yamlfile, default_flow_style=False, allow_unicode=True, sort_keys=False, indent=2)

# Afficher le résumé
print("\n" + "="*80)
print("RÉSUMÉ DU TRAVAIL")
print("="*80)
print(f"Fichier Swagger/OpenAPI traité: {swagger_file}")
print(f"Nombre d'opérations analysées: {stats['operations']}")
print(f"Nombre de séquences de tests créées: {stats['sequences_created']}")
print(f"Nombre total de steps de tests: {stats['steps_created']}")
print(f"\nFichier généré: {output_yaml}")
print("="*80)
