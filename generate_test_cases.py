"""
Génère un blueprint YAML de test cases à partir d'un fichier Swagger/OpenAPI.
Prérequis: PyYAML installé (pip install pyyaml)
Usage: python generate_test_cases.py [--verbose]
"""

import json
import os
import yaml
import argparse
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
    "sequences_created": 0
}

def resolve_schema_ref(schema_ref, definitions):
    """Résout une référence de schéma $ref"""
    if not schema_ref or not definitions:
        if VERBOSE:
            print(f"  [VERBOSE] resolve_schema_ref: schema_ref={schema_ref}, definitions={'present' if definitions else 'None'}")
        return None
    schema_name = schema_ref.split("/")[-1]
    resolved = definitions.get(schema_name)
    if VERBOSE:
        print(f"  [VERBOSE] resolve_schema_ref: {schema_ref} -> {schema_name} -> {'found' if resolved else 'NOT FOUND'}")
    return resolved

def extract_schema_validation(schema, definitions, depth=0, max_depth=50):
    """Extrait les validations complètes d'un schéma (champs + types + objets imbriqués)
    
    Params:
        schema: Schéma à extraire
        definitions: Dictionnaire des définitions de schémas
        depth: Profondeur actuelle de récursion (par défaut 0)
        max_depth: Profondeur maximale autorisée (par défaut 50, set à None pour illimité)
    """
    if not schema or (max_depth is not None and depth > max_depth):
        if VERBOSE:
            print(f"  [VERBOSE] extract_schema_validation: schema={'None' if not schema else 'present'}, depth={depth}, max_depth={max_depth}")
        return None
    
    validation = {}
    
    # Champs requis au niveau racine
    if "properties" in schema:
        # ✅ CORRECTION: Distinguer champs requis vs optionnels
        required_fields = schema.get("required", [])
        all_fields = list(schema["properties"].keys())
        
        validation["required_fields"] = required_fields  # Seulement les champs obligatoires
        validation["all_fields"] = all_fields  # Tous les champs (pour référence)
        validation["field_types"] = {}
        
        if VERBOSE:
            print(f"  [VERBOSE] extract_schema_validation: Found {len(all_fields)} fields: {all_fields}")
            print(f"  [VERBOSE]   Required: {required_fields}")
        
        validation["nested_validations"] = {}
        
        for field_name, field_def in schema["properties"].items():
            field_type = field_def.get("type", "string")
            
            # ✅ CORRECTION: Ne mettre dans field_types que les champs requis
            if field_name in required_fields:
                validation["field_types"][field_name] = field_type
            
            # Gérer les tableaux avec schémas d'objets
            if field_type == "array" and "items" in field_def:
                items_ref = field_def["items"].get("$ref")
                if items_ref:
                    items_schema = resolve_schema_ref(items_ref, definitions)
                    if items_schema:
                        nested_validation = extract_schema_validation(items_schema, definitions, depth + 1, max_depth)
                        if nested_validation:
                            validation["nested_validations"][field_name] = nested_validation
            
            # Gérer les objets imbriqués
            elif field_type == "object":
                ref = field_def.get("$ref")
                if ref:
                    nested_schema = resolve_schema_ref(ref, definitions)
                    if nested_schema:
                        nested_validation = extract_schema_validation(nested_schema, definitions, depth + 1, max_depth)
                        if nested_validation:
                            validation["nested_validations"][field_name] = nested_validation
    
    return validation

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

def create_test_step(label, method, path, parameters=None, body_schema=None, response_schema=None, 
                     status_code="200", phase="action", query_params=None, remove_auth=False,
                     body_override=None, query_override=None, definitions=None, required_body_fields=None):
    """Crée un step de test conforme au schéma api-contract-schema.yml avec validation complète"""
    step = {
        "label": label,
        "phase": phase,
        "request": {
            "method": method.upper(),
            "path": path,
            "headers": {},
            "query": {}
        },
        "expect": {
            "status": int(status_code) if status_code.isdigit() else 200
        }
    }
    
    # Ajouter content_type seulement si le code le justifie (pas 204, 304, 1xx, etc.)
    status_int = int(status_code) if status_code.isdigit() else 200
    if status_int not in [204, 304]:  # 204 No Content, 304 Not Modified
        step["expect"]["content_type"] = "application/json"
    
    # Gérer l'override de query params (pour tests négatifs)
    if query_override is not None:
        step["request"]["query"] = query_override
    elif query_params:
        for param_name, param_info in query_params.items():
            if param_info.get("required"):
                step["request"]["query"][param_name] = f"${{{param_name}}}"
    
    # Gérer l'override du body (pour tests négatifs)
    if body_override is not None:
        step["request"]["body"] = body_override
    elif method.upper() in ["POST", "PUT", "PATCH"] and body_schema:
        step["request"]["body"] = generate_body_example(body_schema, required_fields=required_body_fields)
    
    # Supprimer l'auth pour tests négatifs
    if remove_auth:
        step["request"]["headers"]["Authorization"] = ""
    
    # AMÉLIORATION: Validation de schéma complète avec objets imbriqués
    if VERBOSE:
        print(f"  [VERBOSE] create_test_step: Checking validation - response_schema={'present' if response_schema else 'None'}, status_code={status_code}, startswith_2={status_code.startswith('2') if isinstance(status_code, str) else 'N/A'}")
    if response_schema and status_code.startswith("2"):
        if VERBOSE:
            print(f"  [VERBOSE] create_test_step: Extracting validation for response_schema (status={status_code})...")
        schema_validation = extract_schema_validation(response_schema, definitions)
        if VERBOSE:
            print(f"  [VERBOSE] create_test_step: validation result={'present' if schema_validation else 'None'}")
            if schema_validation:
                print(f"  [VERBOSE]   required_fields={schema_validation.get('required_fields')}")
        if schema_validation:
            # ✅ Champs requis au niveau racine (utiliser required_fields, pas all_fields)
            if "required_fields" in schema_validation and schema_validation["required_fields"]:
                step["expect"]["has_fields"] = schema_validation["required_fields"]
                if VERBOSE:
                    print(f"  [VERBOSE] create_test_step: Added has_fields validation with {len(schema_validation['required_fields'])} required fields")
            
            # Types de données
            if "field_types" in schema_validation:
                step["expect"]["field_types"] = schema_validation["field_types"]
            
            # Validations imbriquées (objets dans tableaux, etc.)
            if "nested_validations" in schema_validation and schema_validation["nested_validations"]:
                step["expect"]["nested_validations"] = schema_validation["nested_validations"]
    
    return step

def generate_body_example(schema, use_invalid_values=False, required_fields=None):
    """Génère un exemple de body JSON à partir d'un schéma"""
    if not schema or not isinstance(schema, dict):
        return "{}"
    
    example = {}
    properties = schema.get("properties", {})
    if required_fields is None:
        required_fields = schema.get("required", [])
    
    for prop_name, prop_def in properties.items():
        # Utiliser les examples du schéma si disponibles
        if "example" in prop_def and not use_invalid_values:
            example[prop_name] = prop_def["example"]
            continue
            
        prop_type = prop_def.get("type", "string")
        
        if use_invalid_values:
            # Générer des valeurs invalides pour les tests négatifs
            if prop_type == "string":
                if prop_def.get("format") == "date-time":
                    example[prop_name] = "invalid-date"
                elif prop_def.get("format") == "email":
                    example[prop_name] = "not-an-email"
                elif prop_def.get("format") == "uuid":
                    example[prop_name] = "not-a-uuid"
                elif "enum" in prop_def:
                    example[prop_name] = "INVALID_ENUM_VALUE"
                else:
                    example[prop_name] = "x" * 10000  # String trop long
            elif prop_type == "integer":
                if "maximum" in prop_def:
                    example[prop_name] = prop_def["maximum"] + 1000
                else:
                    example[prop_name] = -999999
            elif prop_type == "boolean":
                example[prop_name] = "not_a_boolean"
        else:
            # Valeurs valides
            if prop_type == "string":
                if prop_def.get("format") == "date-time":
                    example[prop_name] = "2026-01-09T10:00:00Z"
                elif prop_def.get("format") == "email":
                    example[prop_name] = "test@example.com"
                elif prop_def.get("format") == "uuid":
                    example[prop_name] = "550e8400-e29b-41d4-a716-446655440000"
                elif "enum" in prop_def:
                    example[prop_name] = prop_def["enum"][0]
                else:
                    default_val = prop_def.get("default", f"test_{prop_name}")
                    example[prop_name] = default_val
            elif prop_type == "integer":
                if "minimum" in prop_def:
                    example[prop_name] = prop_def["minimum"]
                elif "default" in prop_def:
                    example[prop_name] = prop_def["default"]
                else:
                    example[prop_name] = 1
            elif prop_type == "boolean":
                example[prop_name] = prop_def.get("default", True)
            elif prop_type == "array":
                example[prop_name] = []
            elif prop_type == "object":
                example[prop_name] = {}
    
    return json.dumps(example, indent=2)

def extract_path_parameters(path):
    """Extrait les paramètres de path {param} et les convertit en ${param}"""
    import re
    # Remplacer {param} par ${param}
    converted_path = re.sub(r'\{([^}]+)\}', r'${\1}', path)
    return converted_path

def create_sequence_from_operation(swagger_data, path, method, operation):
    """Crée une séquence de tests complète pour une opération API"""
    
    # Générer un operationId à partir du summary ou du path si absent
    operation_id = operation.get("operationId")
    summary = operation.get("summary", "")
    
    if not operation_id:
        # Utiliser le summary nettoyé ou un ID généré depuis le path
        if summary:
            # Nettoyer le summary pour faire un ID (ex: "Create or update a tenant" -> "Create_or_update_a_tenant")
            operation_id = summary.replace(" ", "_").replace(".", "").replace(",", "").replace("-", "_")[:50]
        else:
            # Générer depuis le path et la méthode
            clean_path = path.replace("/", "_").replace("{", "").replace("}", "")
            operation_id = f"{method.upper()}{clean_path}"[:50]
    
    if not summary:
        summary = operation_id
    
    # Extraire les tags pour le nom du domaine (utiliser le premier tag ou "API")
    tags = operation.get("tags", ["API"])
    domain_name = tags[0] if tags else "API"
    
    # Convertir le path avec les variables JMeter
    jmeter_path = extract_path_parameters(path)
    
    # Récupérer les définitions pour la résolution de schémas
    # Support à la fois Swagger 2.0 (definitions) et OpenAPI 3.x (components.schemas)
    definitions = swagger_data.get("definitions", {})
    if not definitions and "components" in swagger_data:
        definitions = swagger_data.get("components", {}).get("schemas", {})
    if VERBOSE:
        print(f"[VERBOSE] Operation {operation_id}: Found {len(definitions)} definitions")
        if definitions:
            print(f"[VERBOSE]   Definition keys: {list(definitions.keys())}")
    
    # Extraire les paramètres
    parameters = operation.get("parameters", [])
    if VERBOSE:
        print(f"[VERBOSE] Operation {operation_id}: Found {len(parameters)} parameters")
    path_params = {}
    query_params = {}
    body_schema = None
    
    for param in parameters:
        param_name = param.get("name", "")
        param_in = param.get("in", "")
        
        if param_in == "path":
            path_params[param_name] = param
        elif param_in == "query":
            query_params[param_name] = param
        elif param_in == "body" and "schema" in param:
            # Résoudre le schéma
            schema_ref = param["schema"].get("$ref", "")
            if schema_ref:
                body_schema = resolve_schema_ref(schema_ref, definitions)
    
    # Gérer le requestBody OpenAPI 3.x
    if "requestBody" in operation:
        request_body = operation["requestBody"]
        content = request_body.get("content", {})
        for content_type in ["application/json", "application/hal+json", "*/*"]:
            if content_type in content and "schema" in content[content_type]:
                schema_obj = content[content_type]["schema"]
                schema_ref = schema_obj.get("$ref", "")
                if schema_ref:
                    body_schema = resolve_schema_ref(schema_ref, definitions)
                break
    
    # ✅ Déterminer les champs réellement requis pour cet endpoint
    endpoint_required_body_fields = get_endpoint_required_fields(operation, body_schema)
    if VERBOSE:
        print(f"[VERBOSE] {operation_id}: endpoint_required_body_fields = {endpoint_required_body_fields}")
    
    # Extraire TOUS les codes de réponse et leurs schémas
    responses = operation.get("responses", {})
    response_tests = []
    
    for code, response in responses.items():
        response_schema = None
        
        # Support Swagger 2.0 (direct schema) and OpenAPI 3.x (content.application/json.schema)
        schema_obj = None
        if "schema" in response:
            # Swagger 2.0 format
            schema_obj = response["schema"]
        elif "content" in response:
            # OpenAPI 3.x format
            for content_type in ["application/json", "application/hal+json", "*/*"]:
                if content_type in response["content"] and "schema" in response["content"][content_type]:
                    schema_obj = response["content"][content_type]["schema"]
                    break
        
        if schema_obj:
            schema_ref = schema_obj.get("$ref", "")
            
            # Handle array responses: extract $ref from items
            if not schema_ref and schema_obj.get("type") == "array" and "items" in schema_obj:
                if "$ref" in schema_obj["items"]:
                    schema_ref = schema_obj["items"]["$ref"]
                    if VERBOSE:
                        print(f"[VERBOSE] Response {code}: Found array schema with items $ref={schema_ref}")
            
            if VERBOSE and schema_ref:
                print(f"[VERBOSE] Response {code}: Found schema $ref={schema_ref}")
            
            if schema_ref:
                response_schema = resolve_schema_ref(schema_ref, definitions)
                if VERBOSE:
                    print(f"[VERBOSE] Response {code}: Resolved schema={'present' if response_schema else 'NOT FOUND'}")
        elif VERBOSE:
            print(f"[VERBOSE] Response {code}: No schema found in response")
        
        response_tests.append({
            "code": code,
            "description": response.get("description", f"Status {code}"),
            "schema": response_schema
        })
    
    # Trouver le code de succès
    success_response = next((r for r in response_tests if r["code"].startswith("2")), response_tests[0] if response_tests else {"code": "200", "schema": None})
    
    # Créer la séquence
    sequence = {
        "name": f"{domain_name} - {operation_id}",
        "tags": ["contract", domain_name.split('_')[1] if '_' in domain_name else "api", method.upper()],
        "prereqs": [
            "Authentification requise via token Bearer"
        ],
        "steps": []
    }
    
    # ========== TEST 1: Scénario nominal de succès ==========
    main_step = create_test_step(
        label=f"{method.upper()} {operation_id} - Nominal Success",
        method=method,
        path=jmeter_path,
        query_params=query_params,
        body_schema=body_schema,
        response_schema=success_response["schema"],
        status_code=success_response["code"],
        phase="action",
        definitions=definitions,
        required_body_fields=endpoint_required_body_fields
    )
    sequence["steps"].append(main_step)
    
    # ========== TESTS BASÉS SUR LES RÉPONSES DÉFINIES DANS SWAGGER ==========
    for resp in response_tests:
        code = resp["code"]
        desc = resp["description"]
        
        # Ignorer le code de succès déjà testé
        if code == success_response["code"]:
            continue
        
        # Test pour chaque code d'erreur documenté
        error_step = create_test_step(
            label=f"{method.upper()} {operation_id} - {desc} ({code})",
            method=method,
            path=jmeter_path,
            query_params=query_params if not code.startswith("4") else None,
            body_schema=body_schema if code != "400" else None,
            response_schema=resp["schema"],
            status_code=code,
            phase="action",
            remove_auth=(code == "401"),
            query_override={} if code == "400" else None,
            definitions=definitions,
            required_body_fields=endpoint_required_body_fields
        )
        sequence["steps"].append(error_step)
    
    # ========== TESTS DE PARAMÈTRES REQUIS (un test par paramètre) ==========
    required_query_params = [p for p in parameters if p.get("in") == "query" and p.get("required")]
    for req_param in required_query_params:
        param_name = req_param["name"]
        # Query params sans ce paramètre spécifique
        partial_query = {k: v for k, v in query_params.items() if k != param_name}
        
        missing_param_step = create_test_step(
            label=f"{method.upper()} {operation_id} - Missing Required Query Param: {param_name}",
            method=method,
            path=jmeter_path,
            status_code="400",
            phase="action",
            query_override={k: f"${{{k}}}" for k in partial_query.keys() if query_params[k].get("required")},
            definitions=definitions
        )
        sequence["steps"].append(missing_param_step)
    
    # ========== TESTS DE VALIDATION DE SCHÉMA (body invalide) ==========
    if body_schema and method.upper() in ["POST", "PUT", "PATCH"]:
        properties = body_schema.get("properties", {})
        
        # ✅ Utiliser endpoint_required_body_fields au lieu de tous les required du schéma
        if endpoint_required_body_fields:
            for req_field in endpoint_required_body_fields[:2]:  # Limiter à 2 pour ne pas exploser le nombre de tests
                incomplete_body = json.loads(generate_body_example(body_schema, required_fields=endpoint_required_body_fields))
                incomplete_body.pop(req_field, None)
                
                invalid_body_step = create_test_step(
                    label=f"{method.upper()} {operation_id} - Missing Required Field: {req_field}",
                    method=method,
                    path=jmeter_path,
                    query_params=query_params,
                    status_code="400",
                    phase="action",
                    body_override=json.dumps(incomplete_body, indent=2),
                    definitions=definitions,
                    required_body_fields=endpoint_required_body_fields
                )
                sequence["steps"].append(invalid_body_step)
        
        # Test avec valeurs invalides (enums, formats)
        for prop_name, prop_def in list(properties.items())[:3]:  # Limiter à 3 champs
            if "enum" in prop_def:
                invalid_enum_body = json.loads(generate_body_example(body_schema))
                invalid_enum_body[prop_name] = "INVALID_ENUM_VALUE_NOT_IN_LIST"
                
                enum_test_step = create_test_step(
                    label=f"{method.upper()} {operation_id} - Invalid Enum Value: {prop_name}",
                    method=method,
                    path=jmeter_path,
                    query_params=query_params,
                    status_code="400",
                    phase="action",
                    body_override=json.dumps(invalid_enum_body, indent=2),
                    definitions=definitions
                )
                sequence["steps"].append(enum_test_step)
            
            elif prop_def.get("format") in ["date-time", "email", "uuid"]:
                invalid_format_body = json.loads(generate_body_example(body_schema))
                invalid_format_body[prop_name] = "invalid_format_value"
                
                format_test_step = create_test_step(
                    label=f"{method.upper()} {operation_id} - Invalid Format: {prop_name} ({prop_def['format']})",
                    method=method,
                    path=jmeter_path,
                    query_params=query_params,
                    status_code="400",
                    phase="action",
                    body_override=json.dumps(invalid_format_body, indent=2),
                    definitions=definitions
                )
                sequence["steps"].append(format_test_step)
    
    # ========== TESTS DE VALIDATION DE QUERY PARAMS ==========
    for param_name, param_info in query_params.items():
        # Test enum invalide
        if "enum" in param_info:
            invalid_query = {k: f"${{{k}}}" for k, v in query_params.items() if v.get("required")}
            invalid_query[param_name] = "INVALID_ENUM"
            
            enum_query_step = create_test_step(
                label=f"{method.upper()} {operation_id} - Invalid Query Enum: {param_name}",
                method=method,
                path=jmeter_path,
                status_code="400",
                phase="action",
                query_override=invalid_query,
                definitions=definitions
            )
            sequence["steps"].append(enum_query_step)
    
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
        "name": "Identity API - Contract Tests",
        "description": "Tests de contrat générés automatiquement depuis les spécifications Swagger/OpenAPI",
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
print(f"\nFichier généré: {output_yaml}")
print("="*80)
