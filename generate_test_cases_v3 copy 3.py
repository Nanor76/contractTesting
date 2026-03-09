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
from urllib.parse import urlparse

try:
    import exrex
    HAS_EXREX = True
except ImportError:
    HAS_EXREX = False

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

    def _collect_required_from_composed(self, schema):
        """Collecte tous les champs requis d'un schéma, y compris via allOf et $ref."""
        if not schema:
            return []
        if "$ref" in schema:
            resolved = self.resolve_ref(schema["$ref"])
            return self._collect_required_from_composed(resolved) if resolved else []
        required = list(schema.get("required", []))
        # allOf: union de tous les required (tous les sous-schémas s'appliquent)
        if "allOf" in schema:
            for sub in schema["allOf"]:
                required.extend(self._collect_required_from_composed(sub))
        # oneOf/anyOf: prendre les required du premier sous-schéma
        for composer in ["oneOf", "anyOf"]:
            if composer in schema and schema[composer]:
                required.extend(self._collect_required_from_composed(schema[composer][0]))
                break
        return list(set(required))

    def _parse_status_code(self, status_code):
        """Parse un code de statut, y compris les wildcards (2XX, 4XX, default)."""
        s = str(status_code)
        if s.isdigit():
            return int(s)
        # Wildcard: 2XX → 200, 4XX → 400, 5XX → 500
        if len(s) == 3 and s[0].isdigit() and s[1:].upper() == "XX":
            return int(s[0]) * 100
        return None

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

        # --- GÉNÉRATION DE VALEURS INVALIDES ---
        if use_invalid_values:
            if "composition" in rules:
                return "not_an_object"
            if rules.get("enum"): return "INVALID_ENUM_VALUE_NOT_IN_LIST"
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

        # Schémas composés (allOf / oneOf / anyOf)
        if "composition" in rules:
            if rules["composition"] == "allOf":
                merged = {}
                for sub in rules.get("sub_schemas", []):
                    if sub:
                        sub_ex = self.generate_smart_example(sub)
                        if isinstance(sub_ex, dict):
                            merged.update(sub_ex)
                return merged if merged else {}
            elif rules["composition"] in ("oneOf", "anyOf"):
                for sub in rules.get("sub_schemas", []):
                    if sub:
                        return self.generate_smart_example(sub)
            return None

        if rules.get("enum"): return rules["enum"][0]

        if t == "string":
            fmt = rules.get("format")
            min_len = rules.get("minLength", 0)
            max_len = rules.get("maxLength")
            pattern = rules.get("pattern")

            format_examples = {
                "uuid": "550e8400-e29b-41d4-a716-446655440000",
                "date-time": "2026-03-09T12:00:00Z",
                "email": "test@example.com",
                "uri": "https://example.com/test",
                "url": "https://example.com/test",
                "date": "2026-03-09",
                "ipv4": "192.168.1.1",
                "ipv6": "::1",
            }
            if fmt in format_examples:
                val = format_examples[fmt]
                if max_len is not None and len(val) > max_len:
                    val = val[:max_len]
                return val

            # Tenter de générer une valeur respectant le pattern (pip install exrex)
            if pattern and HAS_EXREX:
                try:
                    val = exrex.getone(pattern)
                    if max_len is not None and len(val) > max_len:
                        val = val[:max_len]
                    if len(val) < min_len:
                        val = val + "a" * (min_len - len(val))
                    return val
                except Exception:
                    pass  # Regex trop complexe, fallback ci-dessous

            # Valeur générique respectant minLength ET maxLength
            base = "test_val"
            if min_len > len(base):
                base = "a" * min_len
            if max_len is not None and len(base) > max_len:
                base = "a" * max(max_len, 1) if max_len >= 1 else ""
            return base

        if t in ["integer", "number"]:
            val = rules.get("minimum", 1)
            # Swagger 2.0: exclusiveMinimum est un booléen
            exc_min = rules.get("exclusiveMinimum")
            if exc_min is True and "minimum" in rules:
                val = rules["minimum"] + 1
            elif isinstance(exc_min, (int, float)):
                # OpenAPI 3.x: exclusiveMinimum est une valeur numérique
                val = exc_min + 1
            # Respecter maximum / exclusiveMaximum
            exc_max = rules.get("exclusiveMaximum")
            if "maximum" in rules and val > rules["maximum"]:
                val = rules["maximum"]
            if exc_max is True and "maximum" in rules and val >= rules["maximum"]:
                val = rules["maximum"] - 1
            elif isinstance(exc_max, (int, float)) and val >= exc_max:
                val = exc_max - 1
            if t == "integer":
                val = int(val)
            return val

        if t == "boolean": return True

        if t == "array":
            item_ex = self.generate_smart_example(rules.get("items"))
            min_items = rules.get("minItems", 1)
            count = max(min_items, 1)
            if item_ex is not None:
                return [copy.deepcopy(item_ex) if isinstance(item_ex, (dict, list)) else item_ex for _ in range(count)]
            return []

        if t == "object":
            obj = {}
            props = rules.get("properties", {})
            for name in rules.get("required", []):
                if name in props:
                    obj[name] = self.generate_smart_example(props[name])
            return obj
        return None

    def get_body_schema(self, operation):
        """Extrait le schéma du body proprement (OpenAPI 3.x & Swagger 2.0).
        Supporte application/json, application/hal+json, */* et résout les $ref."""
        if "requestBody" in operation:
            content = operation["requestBody"].get("content", {})
            for content_type in ["application/json", "application/hal+json", "*/*"]:
                if content_type in content:
                    schema_obj = content[content_type].get("schema")
                    if schema_obj:
                        if "$ref" in schema_obj:
                            return self.resolve_ref(schema_obj["$ref"]) or schema_obj
                        return schema_obj
            return None
        for p in operation.get("parameters", []):
            if p.get("in") == "body" and "schema" in p:
                schema_obj = p["schema"]
                if "$ref" in schema_obj:
                    return self.resolve_ref(schema_obj["$ref"]) or schema_obj
                return schema_obj
        return None

    def get_endpoint_required_fields(self, operation, body_schema):
        """Retourne les champs réellement requis pour cet endpoint.
        Applique la sémantique OpenAPI 3.x: si requestBody.required = false,
        aucun champ du body n'est requis à l'endpoint.
        Collecte les required de tous les sous-schémas allOf/oneOf/anyOf."""
        if not body_schema:
            return []
        if "requestBody" in operation:
            if not operation["requestBody"].get("required", False):
                return []
        return self._collect_required_from_composed(body_schema)

    def resolve_response_schema(self, resp):
        """Extrait et résout le schéma d'une réponse (OpenAPI 3.x & Swagger 2.0)"""
        # OpenAPI 3.x
        content = resp.get("content", {})
        for ct in ["application/json", "application/hal+json", "*/*"]:
            if ct in content:
                schema = content[ct].get("schema")
                if schema:
                    if "$ref" in schema:
                        return self.resolve_ref(schema["$ref"]) or schema
                    return schema
        # Swagger 2.0
        schema = resp.get("schema")
        if schema:
            if "$ref" in schema:
                return self.resolve_ref(schema["$ref"]) or schema
        return schema

    def extract_v1_validation(self, schema, depth=0, max_depth=50):
        """Extrait les validations au format V1 (has_fields / field_types / nested_validations)
        pour compatibilité avec les consommateurs existants."""
        if not schema or depth > max_depth:
            return None

        if "$ref" in schema:
            resolved = self.resolve_ref(schema["$ref"])
            return self.extract_v1_validation(resolved, depth + 1, max_depth) if resolved else None

        # Pour les tableaux, extraire la validation des items
        if schema.get("type") == "array" and "items" in schema:
            items_schema = schema["items"]
            if "$ref" in items_schema:
                items_schema = self.resolve_ref(items_schema["$ref"]) or items_schema
            return self.extract_v1_validation(items_schema, depth + 1, max_depth)

        validation = {}
        if "properties" in schema:
            required_fields = schema.get("required", [])
            all_fields = list(schema["properties"].keys())

            validation["required_fields"] = required_fields
            validation["all_fields"] = all_fields
            validation["field_types"] = {}
            validation["nested_validations"] = {}

            for field_name, field_def in schema["properties"].items():
                field_type = field_def.get("type", "string")
                if field_name in required_fields:
                    validation["field_types"][field_name] = field_type

                # Tableaux avec schémas d'objets
                if field_type == "array" and "items" in field_def:
                    items_schema = field_def["items"]
                    if "$ref" in items_schema:
                        items_schema = self.resolve_ref(items_schema["$ref"]) or items_schema
                    nested = self.extract_v1_validation(items_schema, depth + 1, max_depth)
                    if nested:
                        validation["nested_validations"][field_name] = nested

                # Objets imbriqués
                elif field_type == "object" or "$ref" in field_def:
                    nested_schema = field_def
                    if "$ref" in field_def:
                        nested_schema = self.resolve_ref(field_def["$ref"]) or field_def
                    nested = self.extract_v1_validation(nested_schema, depth + 1, max_depth)
                    if nested:
                        validation["nested_validations"][field_name] = nested

        return validation if validation else None

    def create_step(self, label, method, path, status_code, phase="action",
                    resp_schema_raw=None, body_override=None, query_override=None, 
                    remove_auth=False, req_schema_rules=None, q_params=None):
        """Construit une étape de test individuelle complète"""
        self.stats["steps"] += 1
        jmeter_path = re.sub(r'\{([^}]+)\}', r'${\1}', path)
        status_int = self._parse_status_code(status_code) or 200
        
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
                "status": status_int
            }
        }

        if status_int not in [204, 304]:
            step["expect"]["content_type"] = "application/json"

        # Validations de réponse - seulement pour les succès (2xx)
        if resp_schema_raw and str(status_code).startswith("2"):
            # Format V3 : contract_validation (exhaustif)
            validation_rules = self.extract_exhaustive_schema(resp_schema_raw)
            if validation_rules:
                step["expect"]["contract_validation"] = validation_rules

            # Format V1 : has_fields / field_types / nested_validations (compatibilité)
            v1_validation = self.extract_v1_validation(resp_schema_raw)
            if v1_validation:
                if v1_validation.get("required_fields"):
                    step["expect"]["has_fields"] = v1_validation["required_fields"]
                if v1_validation.get("field_types"):
                    step["expect"]["field_types"] = v1_validation["field_types"]
                if v1_validation.get("nested_validations"):
                    step["expect"]["nested_validations"] = v1_validation["nested_validations"]

        # Remove Auth for 401 tests
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
        """Génère une suite complète et exhaustive de scénarios pour une opération (Nominal + Négatifs)"""
        op_id = operation.get("operationId", f"{method.upper()}_{path.replace('/', '_')}").replace(" ", "_")
        summary = operation.get("summary", "")
        if not op_id:
            if summary:
                op_id = summary.replace(" ", "_").replace(".", "").replace(",", "").replace("-", "_")[:50]
            else:
                clean_path = path.replace("/", "_").replace("{", "").replace("}", "")
                op_id = f"{method.upper()}{clean_path}"[:50]
        if not summary:
            summary = op_id

        tags = operation.get("tags", ["API"])
        domain_name = tags[0] if tags else "API"
        
        sequence = {
            "name": f"{domain_name} - {op_id}",
            "tags": ["contract", domain_name.split('_')[1] if '_' in domain_name else "api", method.upper()],
            "prereqs": [
                "Authentification requise via token Bearer"
            ],
            "steps": []
        }

        # --- Analyse Préliminaire ---
        # Fusionner les paramètres de niveau path ET de niveau opération
        path_level_params = self.swagger_data.get("paths", {}).get(path, {}).get("parameters", [])
        op_params = operation.get("parameters", [])
        # Les paramètres d'opération écrasent ceux du path (par nom + in)
        merged_params = {(p.get("name"), p.get("in")): p for p in path_level_params}
        merged_params.update({(p.get("name"), p.get("in")): p for p in op_params})
        all_params = list(merged_params.values())

        q_params = [p for p in all_params if p.get("in") == "query"]
        req_schema_raw = self.get_body_schema(operation)
        req_schema_rules = self.extract_exhaustive_schema(req_schema_raw) if req_schema_raw else None
        endpoint_required_body_fields = self.get_endpoint_required_fields(operation, req_schema_raw)

        # Construire les règles du body nominal avec les champs endpoint-required
        # (respecte requestBody.required = false => aucun champ requis)
        nominal_schema_rules = None
        if req_schema_rules:
            nominal_schema_rules = copy.deepcopy(req_schema_rules)
            # Ne remplacer "required" que pour les schémas objets, pas les compositions
            if "composition" not in nominal_schema_rules:
                nominal_schema_rules["required"] = endpoint_required_body_fields

        if VERBOSE:
            print(f"  [VERBOSE] {op_id}: body_schema={'yes' if req_schema_raw else 'no'}, "
                  f"required_fields={endpoint_required_body_fields}")

        # Extraire le code de succès et le schéma de réponse (avec résolution $ref)
        success_code = next((code for code in operation.get("responses", {}) if code.startswith("2")), "200")
        success_resp = operation.get("responses", {}).get(success_code, {})
        s_schema = self.resolve_response_schema(success_resp)

        # ==========================================
        # 1. SCÉNARIO NOMINAL (Succès)
        # ==========================================
        sequence["steps"].append(self.create_step(
            label=f"{method.upper()} {op_id} - Nominal Success", 
            method=method, path=path, status_code=success_code, 
            resp_schema_raw=s_schema, req_schema_rules=nominal_schema_rules, q_params=q_params
        ))

        # ==========================================
        # 2. ERREURS DOCUMENTÉES DANS LE SWAGGER
        # ==========================================
        for code, resp in operation.get("responses", {}).items():
            if code.startswith("2") or code == "default":
                continue
            desc = resp.get("description", f"Status {code}")
            e_schema = self.resolve_response_schema(resp)
            
            sequence["steps"].append(self.create_step(
                label=f"{method.upper()} {op_id} - {desc} ({code})", 
                method=method, path=path, status_code=code, 
                resp_schema_raw=e_schema, 
                req_schema_rules=nominal_schema_rules if code != "400" else None, 
                q_params=q_params if not code.startswith("4") else None,
                remove_auth=(code == "401")
            ))

        # ==========================================
        # 3. TESTS NÉGATIFS : QUERY PARAMS
        # ==========================================
        required_queries = [p for p in q_params if p.get("required")]
        
        # A. Paramètres requis manquants
        for req_q in required_queries:
            bad_query = {p["name"]: f"${{{p['name']}}}" for p in required_queries if p["name"] != req_q["name"]}
            sequence["steps"].append(self.create_step(
                label=f"{method.upper()} {op_id} - 400 Missing Query Param: {req_q['name']}",
                method=method, path=path, status_code="400", query_override=bad_query
            ))

        # B. Enums invalides dans query params
        for q in q_params:
            q_schema = q.get("schema", {})
            if "enum" in q_schema or "enum" in q:
                bad_query = {p["name"]: f"${{{p['name']}}}" for p in required_queries}
                bad_query[q["name"]] = "INVALID_ENUM_VALUE"
                sequence["steps"].append(self.create_step(
                    label=f"{method.upper()} {op_id} - 400 Bad Enum Query: {q['name']}",
                    method=method, path=path, status_code="400", query_override=bad_query
                ))

        # C. Mauvais type dans query params
        for q in q_params:
            q_schema = q.get("schema", q)
            q_type = q_schema.get("type")
            if q_type in ["integer", "number"]:
                bad_query = {p["name"]: f"${{{p['name']}}}" for p in required_queries}
                bad_query[q["name"]] = "not_a_number"
                sequence["steps"].append(self.create_step(
                    label=f"{method.upper()} {op_id} - 400 Bad Type Query: {q['name']}",
                    method=method, path=path, status_code="400", query_override=bad_query
                ))

        # ==========================================
        # 4. TESTS NÉGATIFS : BODY (Exhaustif, sans limites artificielles)
        # ==========================================
        if req_schema_rules and method.upper() in ["POST", "PUT", "PATCH"]:
            # Utiliser endpoint_required_body_fields (respecte requestBody.required)
            # et non req_schema_rules["required"] directement
            required_fields = endpoint_required_body_fields
            properties = req_schema_rules.get("properties", {})

            # Construire le body nominal avec uniquement les champs endpoint-required
            nominal_rules = copy.deepcopy(req_schema_rules)
            nominal_rules["required"] = required_fields
            valid_body = self.generate_smart_example(nominal_rules)

            # A. Body vide (seulement si le body a des champs requis à l'endpoint)
            if isinstance(valid_body, dict) and required_fields:
                sequence["steps"].append(self.create_step(
                    label=f"{method.upper()} {op_id} - 400 Empty Body",
                    method=method, path=path, status_code="400",
                    body_override={}, q_params=q_params
                ))

            # B. TOUS les champs requis manquants (un par un, sans limite)
            # Utilise endpoint_required_body_fields, pas schema.required
            if isinstance(valid_body, dict):
                for req_field in required_fields:
                    bad_body = copy.deepcopy(valid_body)
                    bad_body.pop(req_field, None)
                    sequence["steps"].append(self.create_step(
                        label=f"{method.upper()} {op_id} - 400 Missing Body Field: {req_field}",
                        method=method, path=path, status_code="400", body_override=bad_body, q_params=q_params
                    ))

            # C. TOUS les mauvais Formats / Enums (sans limite)
            if isinstance(valid_body, dict) and properties:
                for prop_name, prop_rules in properties.items():
                    if not prop_rules:
                        continue
                    if prop_rules.get("enum") or prop_rules.get("format"):
                        bad_body = copy.deepcopy(valid_body)
                        bad_body[prop_name] = self.generate_smart_example(prop_rules, use_invalid_values=True)
                        sequence["steps"].append(self.create_step(
                            label=f"{method.upper()} {op_id} - 400 Bad Format/Enum: {prop_name}",
                            method=method, path=path, status_code="400", body_override=bad_body, q_params=q_params
                        ))

            # D. Mauvais type de champ (type mismatch)
            if isinstance(valid_body, dict) and properties:
                for prop_name, prop_rules in properties.items():
                    if not prop_rules:
                        continue
                    prop_type = prop_rules.get("type")
                    mismatch_value = None
                    if prop_type in ["integer", "number"]:
                        mismatch_value = "not_a_number"
                    elif prop_type == "boolean":
                        mismatch_value = "not_a_boolean"
                    elif prop_type == "array":
                        mismatch_value = "not_an_array"
                    elif prop_type == "object":
                        mismatch_value = "not_an_object"
                    if mismatch_value is not None:
                        bad_body = copy.deepcopy(valid_body)
                        bad_body[prop_name] = mismatch_value
                        sequence["steps"].append(self.create_step(
                            label=f"{method.upper()} {op_id} - 400 Type Mismatch: {prop_name}",
                            method=method, path=path, status_code="400", body_override=bad_body, q_params=q_params
                        ))

            # E. Valeurs hors limites (boundary: min-1 / max+1)
            if isinstance(valid_body, dict) and properties:
                for prop_name, prop_rules in properties.items():
                    if not prop_rules:
                        continue
                    if "minimum" in prop_rules:
                        bad_body = copy.deepcopy(valid_body)
                        bad_body[prop_name] = prop_rules["minimum"] - 1
                        sequence["steps"].append(self.create_step(
                            label=f"{method.upper()} {op_id} - 400 Below Minimum: {prop_name}",
                            method=method, path=path, status_code="400", body_override=bad_body, q_params=q_params
                        ))
                    if "maximum" in prop_rules:
                        bad_body = copy.deepcopy(valid_body)
                        bad_body[prop_name] = prop_rules["maximum"] + 1
                        sequence["steps"].append(self.create_step(
                            label=f"{method.upper()} {op_id} - 400 Above Maximum: {prop_name}",
                            method=method, path=path, status_code="400", body_override=bad_body, q_params=q_params
                        ))
                    if "maxLength" in prop_rules and prop_rules.get("type") == "string":
                        bad_body = copy.deepcopy(valid_body)
                        bad_body[prop_name] = "x" * (prop_rules["maxLength"] + 1)
                        sequence["steps"].append(self.create_step(
                            label=f"{method.upper()} {op_id} - 400 Exceeds MaxLength: {prop_name}",
                            method=method, path=path, status_code="400", body_override=bad_body, q_params=q_params
                        ))

        # ==========================================
        # 5. TEST NÉGATIF : AUTHENTIFICATION MANQUANTE
        # ==========================================
        # Si 401 n'est pas déjà documenté dans le swagger, l'ajouter
        documented_codes = set(operation.get("responses", {}).keys())
        if "401" not in documented_codes:
            sequence["steps"].append(self.create_step(
                label=f"{method.upper()} {op_id} - 401 Missing Auth",
                method=method, path=path, status_code="401",
                remove_auth=True
            ))

        self.stats["sequences"] += 1
        return sequence

def main():
    DEFAULT_SWAGGER_FILE = "swagger.json"
    parser = argparse.ArgumentParser(description="Générateur de Tests de Contrat API Ultime (V3)")
    parser.add_argument("--input", "-i", default=DEFAULT_SWAGGER_FILE, help=f"Fichier Swagger/OpenAPI JSON source (default: {DEFAULT_SWAGGER_FILE})")
    parser.add_argument("--output", "-o", default="out/api_contract_v3.yml", help="Fichier YAML de sortie")
    parser.add_argument("--verbose", "-v", action="store_true", help="Active le mode verbose/debug")
    args = parser.parse_args()

    global VERBOSE
    VERBOSE = args.verbose

    if VERBOSE:
        print("[VERBOSE] Mode verbose activé")
        print(f"[VERBOSE] Fichier d'entrée: {args.input}")

    if not os.path.exists(args.input):
        print(f"❌ Erreur: Fichier introuvable: {args.input}")
        return

    print(f"🚀 Chargement de {args.input}...")
    with open(args.input, 'r', encoding='utf-8-sig') as f:
        data = json.load(f)

    if "paths" not in data:
        print("❌ Erreur: Fichier Swagger/OpenAPI invalide - section 'paths' manquante")
        return

    expert = ContractExpert(data)
    all_sequences = []

    for path, methods in data.get("paths", {}).items():
        for method, op in methods.items():
            if method.upper() in ["GET", "POST", "PUT", "DELETE", "PATCH"]:
                print(f"  - {method.upper()} {path} ({op.get('operationId', 'N/A')})")
                all_sequences.append(expert.generate_sequence(path, method, op))
                expert.stats["operations"] += 1

    # Extraire la config serveur (OpenAPI 3.x servers OU Swagger 2.0 host/basePath/schemes)
    if "servers" in data and data["servers"]:
        server_url = data["servers"][0].get("url", "https://api.example.com/v1")
        # Résoudre les variables serveur ({var} → valeur par défaut)
        for var_name, var_def in data["servers"][0].get("variables", {}).items():
            server_url = server_url.replace("{" + var_name + "}", var_def.get("default", ""))
        parsed = urlparse(server_url)
        swagger_protocol = parsed.scheme or "https"
        swagger_host = parsed.netloc or "api.example.com"
        swagger_basepath = parsed.path or "/"
    else:
        swagger_host = data.get("host", "api.example.com")
        swagger_basepath = data.get("basePath", "/v1")
        swagger_schemes = data.get("schemes", ["https"])
        swagger_protocol = swagger_schemes[0] if swagger_schemes else "https"

    # Format Riche avec config extraite du swagger
    final_output = {
        "api_contract": {
            "name": "API Contract Tests (Auto-Generated V3 Ultimate)",
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
                    "protocol": swagger_protocol,
                    "host": swagger_host,
                    "basePath": swagger_basepath,
                    "ALLOW_DESTRUCTIVE": "false"
                }
            },
            "sequences": all_sequences
        }
    }

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as yf:
        yaml.dump(final_output, yf, default_flow_style=False, sort_keys=False, indent=2, allow_unicode=True)

    print("\n" + "="*60)
    print("✅ GÉNÉRATION V3 ULTIMATE TERMINÉE AVEC SUCCÈS")
    print("="*60)
    print(f"Endpoints analysés     : {expert.stats['operations']}")
    print(f"Séquences générées     : {expert.stats['sequences']}")
    print(f"Total des steps créés  : {expert.stats['steps']}")
    print(f"Fichier de sortie      : {args.output}")
    print("="*60)

if __name__ == "__main__":
    main()