"""
Générateur de Tests de Contrat API Ultime (V3)
Fusionne l'extraction exhaustive des schémas (V2) et la génération agressive de scénarios négatifs (V1).
Prérequis: pip install pyyaml
Usage: python generate_test_cases_v3.py --input swagger.json
"""

import json
import os
import yaml
import argparse
import re
import copy

VERBOSE = False

class ContractExpert:
    def __init__(self, swagger_data):
        self.swagger_data = swagger_data
        # Support Swagger 2.0 et OpenAPI 3.x
        self.definitions = swagger_data.get("definitions", {})
        if not self.definitions and "components" in swagger_data:
            self.definitions = swagger_data.get("components", {}).get("schemas", {})
        
        self.stats = {"operations": 0, "sequences": 0, "steps": 0}

    def resolve_ref(self, ref):
        """Résout une référence $ref de manière récursive"""
        if not ref or not isinstance(ref, str): return None
        schema_name = ref.split("/")[-1]
        return self.definitions.get(schema_name)

    def extract_exhaustive_schema(self, schema, depth=0, max_depth=20):
        """Extrait TOUTES les contraintes de validation d'un schéma (V2)"""
        if not schema or depth > max_depth: return None

        if "$ref" in schema:
            resolved = self.resolve_ref(schema["$ref"])
            return self.extract_exhaustive_schema(resolved, depth + 1) if resolved else None

        for composer in ["allOf", "anyOf", "oneOf"]:
            if composer in schema:
                return {
                    "composition": composer,
                    "sub_schemas": [self.extract_exhaustive_schema(s, depth + 1) for s in schema[composer]]
                }

        rules = {"type": schema.get("type", "string")}
        if schema.get("nullable") or schema.get("x-nullable"): rules["nullable"] = True

        if rules["type"] == "string":
            for attr in ["format", "pattern", "minLength", "maxLength", "enum"]:
                if attr in schema: rules[attr] = schema[attr]
        elif rules["type"] in ["integer", "number"]:
            for attr in ["minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"]:
                if attr in schema: rules[attr] = schema[attr]
        elif rules["type"] == "array":
            for attr in ["minItems", "maxItems", "uniqueItems"]:
                if attr in schema: rules[attr] = schema[attr]
            if "items" in schema:
                rules["items"] = self.extract_exhaustive_schema(schema["items"], depth + 1)
        elif rules["type"] == "object" or "properties" in schema:
            rules["type"] = "object"
            rules["required"] = schema.get("required", [])
            properties = schema.get("properties", {})
            if properties:
                rules["properties"] = {}
                for p_name, p_def in properties.items():
                    rules["properties"][p_name] = self.extract_exhaustive_schema(p_def, depth + 1)
        
        return rules

    def generate_smart_example(self, rules, use_invalid_values=False):
        """Génère une valeur d'exemple (Valide ou Invalide pour les tests négatifs)"""
        if not rules: return "example"
        
        t = rules.get("type", "string")

        # --- GÉNÉRATION DE VALEURS INVALIDES (V1 logic) ---
        if use_invalid_values:
            if rules.get("enum"): return "INVALID_ENUM_VALUE_NOT_IN_LIST"
            if t == "string":
                fmt = rules.get("format")
                if fmt == "uuid": return "not-a-uuid"
                if fmt == "date-time": return "invalid-date"
                if fmt == "email": return "not-an-email"
                max_len = rules.get("maxLength", 1000)
                return "x" * (max_len + 10) # Dépassement de capacité
            if t in ["integer", "number"]:
                if "maximum" in rules: return rules["maximum"] + 1000
                return -999999
            if t == "boolean": return "not_a_boolean"
            if t == "array": return "not_an_array"
            if t == "object": return "not_an_object"
            return "invalid_value"

        # --- GÉNÉRATION DE VALEURS VALIDES (V2 logic) ---
        if rules.get("enum"): return rules["enum"][0]
        
        if t == "string":
            fmt = rules.get("format")
            if fmt == "uuid": return "550e8400-e29b-41d4-a716-446655440000"
            if fmt == "date-time": return "2026-03-09T12:00:00Z"
            if fmt == "email": return "test@example.com"
            return rules.get("pattern", f"test_val_{rules.get('minLength', '')}")
        
        if t in ["integer", "number"]: return rules.get("minimum", 1)
        if t == "boolean": return True
        if t == "array":
            item_ex = self.generate_smart_example(rules.get("items"))
            return [item_ex] if item_ex else []
        if t == "object":
            obj = {}
            props = rules.get("properties", {})
            for name in rules.get("required", []):
                if name in props:
                    obj[name] = self.generate_smart_example(props[name])
            return obj
        return None

    def get_body_schema(self, operation):
        """Extrait le schéma du body proprement (OpenAPI 3 & Swagger 2)"""
        if "requestBody" in operation:
            return operation["requestBody"].get("content", {}).get("application/json", {}).get("schema")
        for p in operation.get("parameters", []):
            if p.get("in") == "body":
                return p.get("schema")
        return None

    def create_step(self, label, method, path, status_code, phase="action",
                    resp_schema_raw=None, body_override=None, query_override=None, 
                    remove_auth=False, req_schema_rules=None, q_params=None):
        """Construit une étape de test individuelle complète"""
        self.stats["steps"] += 1
        jmeter_path = re.sub(r'\{([^}]+)\}', r'${\1}', path)
        
        step = {
            "label": label,
            "phase": phase,
            "request": {
                "method": method.upper(),
                "path": jmeter_path,
                "headers": {"Accept": "application/json"},
                "query": {}
            },
            "expect": {
                "status": int(status_code)
            }
        }

        if int(status_code) not in [204, 304]:
            step["expect"]["content_type"] = "application/json"

        # Contract Validation (V2) - seulement pour les succès
        if resp_schema_raw and str(status_code).startswith("2"):
            validation_rules = self.extract_exhaustive_schema(resp_schema_raw)
            if validation_rules:
                step["expect"]["contract_validation"] = validation_rules

        # Remove Auth for 401 tests (V1)
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
            body_obj = self.generate_smart_example(req_schema_rules)
            step["request"]["body"] = json.dumps(body_obj, indent=2)
            step["request"]["headers"]["Content-Type"] = "application/json"

        # Cleanup empty dicts
        if not step["request"]["query"]: del step["request"]["query"]

        return step

    def generate_sequence(self, path, method, operation):
        """Génère une suite complète de scénarios pour une opération (Nominal + Négatifs)"""
        op_id = operation.get("operationId", f"{method.upper()}_{path.replace('/', '_')}").replace(" ", "_")
        tags = operation.get("tags", ["API"])
        
        sequence = {
            "name": f"{tags[0]} - {op_id}",
            "tags": ["contract", method.upper()],
            "steps": []
        }

        # --- Analyse Préliminaire ---
        q_params = [p for p in operation.get("parameters", []) if p.get("in") == "query"]
        req_schema_raw = self.get_body_schema(operation)
        req_schema_rules = self.extract_exhaustive_schema(req_schema_raw) if req_schema_raw else None

        success_code = next((code for code in operation.get("responses", {}) if code.startswith("2")), "200")
        success_resp = operation.get("responses", {}).get(success_code, {})
        s_schema = success_resp.get("content", {}).get("application/json", {}).get("schema") or success_resp.get("schema")

        # ==========================================
        # 1. SCÉNARIO NOMINAL (Succès)
        # ==========================================
        sequence["steps"].append(self.create_step(
            label=f"{method.upper()} {op_id} - Success Nominal", 
            method=method, path=path, status_code=success_code, 
            resp_schema_raw=s_schema, req_schema_rules=req_schema_rules, q_params=q_params
        ))

        # ==========================================
        # 2. ERREURS DOCUMENTÉES DANS LE SWAGGER
        # ==========================================
        for code, resp in operation.get("responses", {}).items():
            if code.startswith("2") or code == "default": continue
            e_schema = resp.get("content", {}).get("application/json", {}).get("schema") or resp.get("schema")
            
            sequence["steps"].append(self.create_step(
                label=f"{method.upper()} {op_id} - Documented Error {code}", 
                method=method, path=path, status_code=code, 
                resp_schema_raw=e_schema, req_schema_rules=req_schema_rules if code != "400" else None, 
                q_params=q_params if not code.startswith("4") else None,
                remove_auth=(code == "401")
            ))

        # ==========================================
        # 3. TESTS NÉGATIFS : QUERY PARAMS (V1)
        # ==========================================
        required_queries = [p for p in q_params if p.get("required")]
        for req_q in required_queries:
            bad_query = {p["name"]: f"${{{p['name']}}}" for p in required_queries if p["name"] != req_q["name"]}
            sequence["steps"].append(self.create_step(
                label=f"{method.upper()} {op_id} - 400 Missing Query Param: {req_q['name']}",
                method=method, path=path, status_code="400", query_override=bad_query
            ))

        for q in q_params:
            if "enum" in q.get("schema", {}) or "enum" in q:
                bad_query = {p["name"]: f"${{{p['name']}}}" for p in required_queries}
                bad_query[q["name"]] = "INVALID_ENUM_VALUE"
                sequence["steps"].append(self.create_step(
                    label=f"{method.upper()} {op_id} - 400 Bad Enum Query: {q['name']}",
                    method=method, path=path, status_code="400", query_override=bad_query
                ))

        # ==========================================
        # 4. TESTS NÉGATIFS : BODY (V1)
        # ==========================================
        if req_schema_rules and method.upper() in ["POST", "PUT", "PATCH"]:
            required_fields = req_schema_rules.get("required", [])
            properties = req_schema_rules.get("properties", {})
            valid_body = self.generate_smart_example(req_schema_rules)

            # A. Champs requis manquants (limité à 2 pour ne pas surcharger)
            if isinstance(valid_body, dict):
                for req_field in required_fields[:2]:
                    bad_body = copy.deepcopy(valid_body)
                    bad_body.pop(req_field, None)
                    sequence["steps"].append(self.create_step(
                        label=f"{method.upper()} {op_id} - 400 Missing Body Field: {req_field}",
                        method=method, path=path, status_code="400", body_override=bad_body, q_params=q_params
                    ))

            # B. Mauvais Formats / Enums (limité à 3)
            if isinstance(valid_body, dict):
                test_count = 0
                for prop_name, prop_rules in properties.items():
                    if test_count >= 3: break
                    if prop_rules and (prop_rules.get("enum") or prop_rules.get("format")):
                        bad_body = copy.deepcopy(valid_body)
                        bad_body[prop_name] = self.generate_smart_example(prop_rules, use_invalid_values=True)
                        sequence["steps"].append(self.create_step(
                            label=f"{method.upper()} {op_id} - 400 Bad Format/Enum Body Field: {prop_name}",
                            method=method, path=path, status_code="400", body_override=bad_body, q_params=q_params
                        ))
                        test_count += 1

        self.stats["sequences"] += 1
        return sequence

def main():
    parser = argparse.ArgumentParser(description="Générateur de Tests de Contrat API Ultime (V3)")
    parser.add_argument("--input", "-i", required=True, help="Fichier Swagger/OpenAPI JSON source")
    parser.add_argument("--output", "-o", default="out/api_contract_v3.yml", help="Fichier YAML de sortie")
    args = parser.parse_args()

    if not os.path.exists(args.input):
        print(f"❌ Erreur: Fichier introuvable: {args.input}")
        return

    print(f"🚀 Chargement de {args.input}...")
    with open(args.input, 'r', encoding='utf-8-sig') as f:
        data = json.load(f)

    expert = ContractExpert(data)
    all_sequences = []

    for path, methods in data.get("paths", {}).items():
        for method, op in methods.items():
            if method.upper() in ["GET", "POST", "PUT", "DELETE", "PATCH"]:
                all_sequences.append(expert.generate_sequence(path, method, op))
                expert.stats["operations"] += 1

    # Format Riche de la V1 réintégré
    final_output = {
        "api_contract": {
            "name": "API Contract Tests (Auto-Generated V3)",
            "description": "Tests générés combinant validation sémantique profonde et tests négatifs exhaustifs.",
            "config": {
                "base_url": "${protocol}://${host}${basePath}",
                "auth_token": "${token}",
                "default_headers": {
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Authorization": "Bearer ${token}"
                },
                "variables": {
                    "protocol": "https",
                    "host": "api.example.com",
                    "basePath": "/v1",
                    "ALLOW_DESTRUCTIVE": "false"
                }
            },
            "sequences": all_sequences
        }
    }

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as yf:
        yaml.dump(final_output, yf, default_flow_style=False, sort_keys=False, indent=2, allow_unicode=True)

    print("\n" + "="*50)
    print("✅ GÉNÉRATION V3 TERMINÉE AVEC SUCCÈS")
    print("="*50)
    print(f"Endpoints analysés   : {expert.stats['operations']}")
    print(f"Séquences générées   : {expert.stats['sequences']}")
    print(f"Total des tests créés: {expert.stats['steps']}")
    print(f"Fichier de sortie    : {args.output}")
    print("="*50)

if __name__ == "__main__":
    main()