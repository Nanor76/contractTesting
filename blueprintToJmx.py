#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Convertit un blueprint YAML de tests de contrat en fichier JMeter JMX.
Prérequis: PyYAML installé (pip install pyyaml)
Usage: python blueprintToJmx.py [--yml <file>] [--out <dir|file>]

Génère un plan de test JMeter complet avec assertions JSON et captures de variables.

YAML attendu (extrait) :
api_contract:
  name: ...
  config:
    variables: { protocol, host, port, basePath, ... }
    default_headers: { Accept, Content-Type, Authorization? }
  sequences:
    - name: ...
      steps:
        - label: ...
          enabled: true | false | "${ALLOW_DESTRUCTIVE}"
          request: { method, path, headers?, query?, body? }
          expect: { status, content_type?, has_fields?, matches? }
          capture: [ { json_path: "$.id", as: "var" }, ... ]

Usage:
  pip install pyyaml
  python generate_jmx_from_yml.py --yml tools/jmxgen/contract-test-definition.yml --out out/contract_stateful.jmx
"""

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import xml.etree.ElementTree as ET
from xml.dom import minidom

try:
    import yaml  # PyYAML
except Exception as e:
    yaml = None


# ---------------------------
# XML helpers
# ---------------------------

def _el(tag: str, attrs: Optional[Dict[str, str]] = None) -> ET.Element:
    e = ET.Element(tag)
    if attrs:
        for k, v in attrs.items():
            e.set(k, v)
    return e


def _prop(parent: ET.Element, tag: str, name: str, value: Any) -> None:
    p = ET.SubElement(parent, tag)
    p.set("name", name)
    p.text = "" if value is None else str(value)


def _pretty_xml_bytes(root: ET.Element) -> bytes:
    rough = ET.tostring(root, encoding="utf-8")
    dom = minidom.parseString(rough)
    return dom.toprettyxml(indent="  ", encoding="UTF-8")


def _bool(v: bool) -> str:
    return "true" if v else "false"


# ---------------------------
# Minimal JMeter elements
# ---------------------------

def create_testplan(name: str, comments: str = "") -> ET.Element:
    tp = _el("TestPlan", {
        "guiclass": "TestPlanGui",
        "testclass": "TestPlan",
        "testname": name,
        "enabled": "true"
    })
    _prop(tp, "stringProp", "TestPlan.comments", comments)
    _prop(tp, "boolProp", "TestPlan.functional_mode", "false")
    _prop(tp, "boolProp", "TestPlan.serialize_threadgroups", "false")

    # User Defined Variables container
    udv = ET.SubElement(tp, "elementProp", {
        "name": "TestPlan.user_defined_variables",
        "elementType": "Arguments",
        "guiclass": "ArgumentsPanel",
        "testclass": "Arguments",
        "testname": "User Defined Variables",
        "enabled": "true"
    })
    ET.SubElement(udv, "collectionProp", {"name": "Arguments.arguments"})
    return tp


def udv_collection_from_testplan(tp: ET.Element) -> ET.Element:
    # Find collectionProp "Arguments.arguments" under TestPlan.user_defined_variables
    for ep in tp.findall(".//elementProp"):
        if ep.get("name") == "TestPlan.user_defined_variables":
            coll = ep.find("./collectionProp[@name='Arguments.arguments']")
            if coll is None:
                raise RuntimeError("UDV collection not found")
            return coll
    raise RuntimeError("UDV container not found")


def add_user_var(udv_coll: ET.Element, name: str, value: str, desc: str = "") -> None:
    ep = ET.SubElement(udv_coll, "elementProp", {"name": name, "elementType": "Argument"})
    _prop(ep, "stringProp", "Argument.name", name)
    _prop(ep, "stringProp", "Argument.value", value)
    _prop(ep, "stringProp", "Argument.metadata", "=")
    if desc:
        _prop(ep, "stringProp", "Argument.desc", desc)


def create_global_header_manager(headers: Dict[str, str], exclude_auth: bool = True) -> ET.Element:
    hm = _el("HeaderManager", {
        "guiclass": "HeaderPanel",
        "testclass": "HeaderManager",
        "testname": "HTTP Headers (Global)",
        "enabled": "true"
    })
    coll = ET.SubElement(hm, "collectionProp", {"name": "HeaderManager.headers"})
    for k, v in (headers or {}).items():
        if exclude_auth and k.lower() == "authorization":
            continue
        ep = ET.SubElement(coll, "elementProp", {"name": "", "elementType": "Header"})
        _prop(ep, "stringProp", "Header.name", k)
        _prop(ep, "stringProp", "Header.value", v)
    return hm


def create_local_header_manager(name: str, headers: Dict[str, str]) -> ET.Element:
    hm = _el("HeaderManager", {
        "guiclass": "HeaderPanel",
        "testclass": "HeaderManager",
        "testname": name,
        "enabled": "true"
    })
    coll = ET.SubElement(hm, "collectionProp", {"name": "HeaderManager.headers"})
    for k, v in (headers or {}).items():
        if v is None:
            continue
        ep = ET.SubElement(coll, "elementProp", {"name": "", "elementType": "Header"})
        _prop(ep, "stringProp", "Header.name", k)
        _prop(ep, "stringProp", "Header.value", v)
    return hm


def create_thread_group(name: str, threads: int = 1, ramp_time: int = 1, loops: int = 1) -> ET.Element:
    tg = _el("ThreadGroup", {
        "guiclass": "ThreadGroupGui",
        "testclass": "ThreadGroup",
        "testname": name,
        "enabled": "true"
    })
    _prop(tg, "stringProp", "ThreadGroup.on_sample_error", "continue")
    _prop(tg, "stringProp", "ThreadGroup.num_threads", str(threads))
    _prop(tg, "stringProp", "ThreadGroup.ramp_time", str(ramp_time))
    _prop(tg, "boolProp", "ThreadGroup.scheduler", "false")

    loop = ET.SubElement(tg, "elementProp", {
        "name": "ThreadGroup.main_controller",
        "elementType": "LoopController",
        "guiclass": "LoopControlPanel",
        "testclass": "LoopController",
        "testname": "Loop Controller",
        "enabled": "true"
    })
    _prop(loop, "boolProp", "LoopController.continue_forever", "false")
    _prop(loop, "stringProp", "LoopController.loops", str(loops))
    return tg


def create_transaction_controller(name: str) -> ET.Element:
    tc = _el("TransactionController", {
        "guiclass": "TransactionControllerGui",
        "testclass": "TransactionController",
        "testname": name,
        "enabled": "true"
    })
    _prop(tc, "boolProp", "TransactionController.includeTimers", "false")
    _prop(tc, "boolProp", "TransactionController.parent", "false")
    return tc


def create_generic_controller(name: str) -> ET.Element:
    gc = _el("GenericController", {
        "guiclass": "LogicControllerGui",
        "testclass": "GenericController",
        "testname": name,
        "enabled": "true"
    })
    return gc


def create_if_controller(name: str, var_expr: str) -> ET.Element:
    """
    enabled: "${ALLOW_DESTRUCTIVE}" => IfController condition: vars.get('ALLOW_DESTRUCTIVE') == 'true'
    """
    ic = _el("IfController", {
        "guiclass": "IfControllerPanel",
        "testclass": "IfController",
        "testname": name,
        "enabled": "true"
    })
    # Condition groovy
    # Note: __groovy return "true"/"false" is acceptable for IfController
    _prop(ic, "stringProp", "IfController.condition",
          f"${{__groovy((vars.get('{var_expr}') ?: 'false').toLowerCase() == 'true')}}")
    _prop(ic, "boolProp", "IfController.evaluateAll", "false")
    return ic


def create_http_sampler(label: str, method: str, path: str, query: Dict[str, Any], body: Optional[str]) -> ET.Element:
    s = _el("HTTPSamplerProxy", {
        "guiclass": "HttpTestSampleGui",
        "testclass": "HTTPSamplerProxy",
        "testname": label,
        "enabled": "true"
    })

    # Server parts from UDV vars
    _prop(s, "stringProp", "HTTPSampler.protocol", "${protocol}")
    _prop(s, "stringProp", "HTTPSampler.domain", "${host}")
    _prop(s, "stringProp", "HTTPSampler.port", "${port}")
    _prop(s, "stringProp", "HTTPSampler.path", "${basePath}" + (path or ""))
    _prop(s, "stringProp", "HTTPSampler.method", method)
    _prop(s, "boolProp", "HTTPSampler.follow_redirects", "true")
    _prop(s, "boolProp", "HTTPSampler.use_keepalive", "true")

    if body is not None:
        _prop(s, "boolProp", "HTTPSampler.postBodyRaw", "true")
        args = ET.SubElement(s, "elementProp", {"name": "HTTPsampler.Arguments", "elementType": "Arguments"})
        coll = ET.SubElement(args, "collectionProp", {"name": "Arguments.arguments"})
        arg = ET.SubElement(coll, "elementProp", {"name": "", "elementType": "HTTPArgument"})
        _prop(arg, "boolProp", "HTTPArgument.always_encode", "false")
        _prop(arg, "stringProp", "Argument.value", body)
        _prop(arg, "stringProp", "Argument.metadata", "=")
    else:
        if query:
            args = ET.SubElement(s, "elementProp", {"name": "HTTPsampler.Arguments", "elementType": "Arguments"})
            coll = ET.SubElement(args, "collectionProp", {"name": "Arguments.arguments"})
            for k, v in query.items():
                arg = ET.SubElement(coll, "elementProp", {"name": str(k), "elementType": "HTTPArgument"})
                _prop(arg, "boolProp", "HTTPArgument.always_encode", "true")
                _prop(arg, "stringProp", "Argument.name", str(k))
                _prop(arg, "stringProp", "Argument.value", "" if v is None else str(v))
                _prop(arg, "stringProp", "Argument.metadata", "=")

    return s


def add_response_code_assertion(parent_tree: ET.Element, expected: str) -> None:
    a = _el("ResponseAssertion", {
        "guiclass": "AssertionGui",
        "testclass": "ResponseAssertion",
        "testname": "Response Code",
        "enabled": "true"
    })
    _prop(a, "stringProp", "Assertion.test_field", "Assertion.response_code")
    _prop(a, "boolProp", "Assertion.assume_success", "false")
    _prop(a, "intProp", "Assertion.test_type", "8")  # equals
    coll = ET.SubElement(a, "collectionProp", {"name": "Asserion.test_strings"})
    _prop(coll, "stringProp", "0", str(expected))
    parent_tree.append(a)
    parent_tree.append(_el("hashTree"))


def add_content_type_assertion(parent_tree: ET.Element, expected_contains: str) -> None:
    a = _el("ResponseAssertion", {
        "guiclass": "AssertionGui",
        "testclass": "ResponseAssertion",
        "testname": "Content-Type Header",
        "enabled": "true"
    })
    _prop(a, "stringProp", "Assertion.test_field", "Assertion.response_headers")
    _prop(a, "boolProp", "Assertion.assume_success", "false")
    _prop(a, "intProp", "Assertion.test_type", "16")  # contains
    coll = ET.SubElement(a, "collectionProp", {"name": "Asserion.test_strings"})
    _prop(coll, "stringProp", "0", expected_contains)
    parent_tree.append(a)
    parent_tree.append(_el("hashTree"))


def _groovy_json_getter_fn() -> str:
    # Utilisation de la syntaxe /.../ (Slashy String) de Groovy pour le split
    # Cela évite les erreurs d'échappement de backslash dans la regex du point
    return r"""
def __getByPath(def obj, String jsonPath) {
  if (obj == null) return null
  if (jsonPath == null) return null
  def p = jsonPath.trim()
  if (!p.startsWith('$.')) return null
  // On utilise /\./ pour dire "le caractère point littéral" en Groovy
  def parts = p.substring(2).split(/\./)
  def cur = obj
  for (def part : parts) {
    if (cur == null) return null
    if (cur instanceof Map) {
      cur = cur.get(part)
    } else {
      return null
    }
  }
  return cur
}
""".strip()


def add_jsr223_assert_has_fields(parent_tree: ET.Element, fields: List[str]) -> None:
    if not fields:
        return
    script = [
        "import groovy.json.JsonSlurper",
        "",
        _groovy_json_getter_fn(),
        "",
        "def s = prev.getResponseDataAsString()",
        "assert s != null && s.trim().length() > 0 : 'Empty response body'",
        "def json = new JsonSlurper().parseText(s)",
    ]
    for f in fields:
        jp = f if f.startswith("$.") else f"$.{f}"
        # Utilisation de guillemets simples (') pour éviter l'interprétation du $ par Groovy
        script.append(f"assert __getByPath(json, '{jp}') != null : 'Missing required field: {jp}'")

    a = _el("JSR223Assertion", {
        "guiclass": "TestBeanGUI",
        "testclass": "JSR223Assertion",
        "testname": "Check required fields",
        "enabled": "true"
    })
    _prop(a, "stringProp", "scriptLanguage", "groovy")
    _prop(a, "stringProp", "script", "\n".join(script))
    parent_tree.append(a)
    parent_tree.append(_el("hashTree"))


def add_jsr223_assert_field_types(parent_tree: ET.Element, field_types: Dict[str, str]) -> None:
    """Valide les types de données des champs selon le contrat"""
    if not field_types:
        return
    
    script = [
        "import groovy.json.JsonSlurper",
        "",
        _groovy_json_getter_fn(),
        "",
        "def s = prev.getResponseDataAsString()",
        "assert s != null && s.trim().length() > 0 : 'Empty response body'",
        "def json = new JsonSlurper().parseText(s)",
        "",
    ]
    
    for field_name, expected_type in field_types.items():
        jp = field_name if field_name.startswith("$.") else f"$.{field_name}"
        script.append(f"def val_{field_name.replace('.', '_')} = __getByPath(json, '{jp}')")
        script.append(f"if (val_{field_name.replace('.', '_')} != null) {{")
        
        if expected_type == "string":
            script.append(f"  assert val_{field_name.replace('.', '_')} instanceof String : 'Field {jp} should be string, got ' + val_{field_name.replace('.', '_')}.getClass().getName()")
        elif expected_type == "integer":
            script.append(f"  assert val_{field_name.replace('.', '_')} instanceof Integer || val_{field_name.replace('.', '_')} instanceof Long : 'Field {jp} should be integer, got ' + val_{field_name.replace('.', '_')}.getClass().getName()")
        elif expected_type == "boolean":
            script.append(f"  assert val_{field_name.replace('.', '_')} instanceof Boolean : 'Field {jp} should be boolean, got ' + val_{field_name.replace('.', '_')}.getClass().getName()")
        elif expected_type == "array":
            script.append(f"  assert val_{field_name.replace('.', '_')} instanceof List : 'Field {jp} should be array, got ' + val_{field_name.replace('.', '_')}.getClass().getName()")
        elif expected_type == "object":
            script.append(f"  assert val_{field_name.replace('.', '_')} instanceof Map : 'Field {jp} should be object, got ' + val_{field_name.replace('.', '_')}.getClass().getName()")
        elif expected_type == "number":
            script.append(f"  assert val_{field_name.replace('.', '_')} instanceof Number : 'Field {jp} should be number, got ' + val_{field_name.replace('.', '_')}.getClass().getName()")
        
        script.append("}")
    
    a = _el("JSR223Assertion", {
        "guiclass": "TestBeanGUI",
        "testclass": "JSR223Assertion",
        "testname": "Validate field types",
        "enabled": "true"
    })
    _prop(a, "stringProp", "scriptLanguage", "groovy")
    _prop(a, "stringProp", "script", "\n".join(script))
    parent_tree.append(a)
    parent_tree.append(_el("hashTree"))


def add_jsr223_assert_nested_validations(parent_tree: ET.Element, nested_validations: Dict[str, Any]) -> None:
    """Valide les objets imbriqués dans les tableaux (ex: items dans salesPersons[])"""
    if not nested_validations:
        return
    
    script = [
        "import groovy.json.JsonSlurper",
        "",
        _groovy_json_getter_fn(),
        "",
        "def s = prev.getResponseDataAsString()",
        "assert s != null && s.trim().length() > 0 : 'Empty response body'",
        "def json = new JsonSlurper().parseText(s)",
        "",
    ]
    
    for field_name, validation_schema in nested_validations.items():
        jp = field_name if field_name.startswith("$.") else f"$.{field_name}"
        
        # Récupérer le tableau
        script.append(f"def array_{field_name.replace('.', '_')} = __getByPath(json, '{jp}')")
        script.append(f"assert array_{field_name.replace('.', '_')} instanceof List : 'Field {jp} should be an array'")
        script.append(f"")
        script.append(f"// Validate each item in {jp}")
        script.append(f"if (array_{field_name.replace('.', '_')}.size() > 0) {{")
        script.append(f"  array_{field_name.replace('.', '_')}.eachWithIndex {{ item, idx ->")
        
        # Valider les champs requis dans chaque élément
        if "has_fields" in validation_schema:
            for req_field in validation_schema["has_fields"]:
                script.append(f"    assert item.containsKey('{req_field}') : '{jp}[' + idx + '] missing required field: {req_field}'")
                script.append(f"    assert item['{req_field}'] != null : '{jp}[' + idx + '].{req_field} is null'")
        
        # Valider les types des champs
        if "field_types" in validation_schema:
            for field, ftype in validation_schema["field_types"].items():
                script.append(f"    if (item.containsKey('{field}') && item['{field}'] != null) {{")
                
                if ftype == "string":
                    script.append(f"      assert item['{field}'] instanceof String : '{jp}[' + idx + '].{field} should be string, got ' + item['{field}'].getClass().getName()")
                elif ftype == "integer":
                    script.append(f"      assert item['{field}'] instanceof Integer || item['{field}'] instanceof Long : '{jp}[' + idx + '].{field} should be integer, got ' + item['{field}'].getClass().getName()")
                elif ftype == "boolean":
                    script.append(f"      assert item['{field}'] instanceof Boolean : '{jp}[' + idx + '].{field} should be boolean, got ' + item['{field}'].getClass().getName()")
                elif ftype == "array":
                    script.append(f"      assert item['{field}'] instanceof List : '{jp}[' + idx + '].{field} should be array, got ' + item['{field}'].getClass().getName()")
                elif ftype == "object":
                    script.append(f"      assert item['{field}'] instanceof Map : '{jp}[' + idx + '].{field} should be object, got ' + item['{field}'].getClass().getName()")
                
                script.append(f"    }}")
        
        script.append(f"  }}")
        script.append(f"}}")
        script.append(f"")
    
    a = _el("JSR223Assertion", {
        "guiclass": "TestBeanGUI",
        "testclass": "JSR223Assertion",
        "testname": "Validate nested objects (array items)",
        "enabled": "true"
    })
    _prop(a, "stringProp", "scriptLanguage", "groovy")
    _prop(a, "stringProp", "script", "\n".join(script))
    parent_tree.append(a)
    parent_tree.append(_el("hashTree"))


def add_jsr223_assert_matches(parent_tree: ET.Element, matches: List[Dict[str, Any]]) -> None:
    if not matches:
        return
    script = [
        "import groovy.json.JsonSlurper",
        "",
        _groovy_json_getter_fn(),
        "",
        "def s = prev.getResponseDataAsString()",
        "assert s != null && s.trim().length() > 0 : 'Empty response body'",
        "def json = new JsonSlurper().parseText(s)",
        "",
    ]
    for m in matches:
        jp = m.get("json_path") or m.get("jsonPath") or ""
        expected = m.get("expected_value") or m.get("expected") or ""
        if not jp:
            continue
        
        script.append(f"def v = __getByPath(json, '{jp}')")
        script.append(f"assert v != null : 'Null value at {jp}'")
        # Correction ici : Utilisation de guillemets simples pour les chaînes littérales Groovy
        script.append(f"assert String.valueOf(v) == '{expected}' : 'Mismatch at {jp}: expected={expected}, got=' + v")

    a = _el("JSR223Assertion", {
        "guiclass": "TestBeanGUI",
        "testclass": "JSR223Assertion",
        "testname": "Check JSON matches",
        "enabled": "true"
    })

    _prop(a, "stringProp", "scriptLanguage", "groovy")
    _prop(a, "stringProp", "script", "\n".join(script))
    parent_tree.append(a)
    parent_tree.append(_el("hashTree"))


def add_jsr223_postprocessor_capture(parent_tree: ET.Element, captures: List[Dict[str, Any]]) -> None:
    if not captures:
        return

    script = [
        "import groovy.json.JsonSlurper",
        "",
        _groovy_json_getter_fn(),
        "",
        "def s = prev.getResponseDataAsString()",
        "if (s == null || s.trim().isEmpty()) { return }",
        "def json = new JsonSlurper().parseText(s)",
        "",
    ]
    for c in captures:
        jp = c.get("json_path") or c.get("jsonPath") or ""
        var = c.get("as") or c.get("var") or ""
        if not jp or not var:
            continue
        script.append(f"def cv = __getByPath(json, '{jp}')")
        script.append(f"if (cv != null) vars.put('{var}', String.valueOf(cv))")

    pp = _el("JSR223PostProcessor", {
        "guiclass": "TestBeanGUI",
        "testclass": "JSR223PostProcessor",
        "testname": "Capture variables",
        "enabled": "true"
    })
    _prop(pp, "stringProp", "scriptLanguage", "groovy")
    _prop(pp, "stringProp", "script", "\n".join(script))
    parent_tree.append(pp)
    parent_tree.append(_el("hashTree"))


# ---------------------------
# YAML -> JMX
# ---------------------------

def _normalize_content_type(ct: str) -> str:
    if not ct:
        return ""
    c = ct.strip().lower()
    if c in ("json", "application/json"):
        return "application/json"
    return ct


def build_jmx_from_yaml(doc: Dict[str, Any]) -> Tuple[ET.Element, Dict[str, int]]:
    api = (doc or {}).get("api_contract") or {}
    name = api.get("name") or "Contract Tests (Stateful)"
    cfg = api.get("config") or {}
    variables = (cfg.get("variables") or {})
    default_headers = (cfg.get("default_headers") or {})

    stats = {"sequences": 0, "steps": 0, "samplers": 0}

    # Root
    root = _el("jmeterTestPlan", {"version": "1.2", "properties": "5.0", "jmeter": "5.6.3"})
    root_tree = ET.SubElement(root, "hashTree")

    # TestPlan
    tp = create_testplan(name, comments="Generated from contract-test-definition.yml")
    root_tree.append(tp)
    tp_tree = _el("hashTree")
    root_tree.append(tp_tree)

    # UDV
    udv_coll = udv_collection_from_testplan(tp)
    # Variables nécessaires (au minimum protocol/host/port/basePath)
    for k, v in variables.items():
        add_user_var(udv_coll, str(k), "" if v is None else str(v), desc=f"YAML var: {k}")

    # Si token pas déjà présent : ajouter token placeholder
    if "token" not in variables and "${token}" in str(cfg.get("auth_token", "")):
        add_user_var(udv_coll, "token", "<A_RENSEIGNER>", desc="Auth token")

    # Thread Group (simple)
    tg = create_thread_group("Contract Tests (Stateful)", threads=1, ramp_time=1, loops=1)
    tp_tree.append(tg)
    tg_tree = _el("hashTree")
    tp_tree.append(tg_tree)

    # Global headers (au niveau ThreadGroup, pas TestPlan)
    tg_tree.append(create_global_header_manager(default_headers, exclude_auth=True))
    tg_tree.append(_el("hashTree"))

    sequences = api.get("sequences") or []
    for seq in sequences:
        stats["sequences"] += 1
        seq_name = seq.get("name") or "Sequence"
        tc_seq = create_transaction_controller(seq_name)
        tg_tree.append(tc_seq)
        seq_tree = _el("hashTree")
        tg_tree.append(seq_tree)

        # Group by phase for readability
        steps = seq.get("steps") or []
        phases_order = ["setup", "action", "cleanup", None, ""]
        phase_map: Dict[str, List[Dict[str, Any]]] = {"setup": [], "action": [], "cleanup": [], "other": []}
        for st in steps:
            ph = (st.get("phase") or "").strip().lower()
            if ph in ("setup", "action", "cleanup"):
                phase_map[ph].append(st)
            else:
                phase_map["other"].append(st)

        for ph in ["setup", "action", "cleanup", "other"]:
            if not phase_map[ph]:
                continue
            block = create_generic_controller(ph.upper() if ph != "other" else "STEPS")
            seq_tree.append(block)
            block_tree = _el("hashTree")
            seq_tree.append(block_tree)

            for st in phase_map[ph]:
                stats["steps"] += 1

                label = st.get("label") or "Step"
                enabled = st.get("enabled", True)

                req = st.get("request") or {}
                method = (req.get("method") or "GET").upper()
                path = req.get("path") or "/"
                headers = req.get("headers") or {}
                query = req.get("query") or {}
                body = req.get("body", None)

                exp = st.get("expect") or {}
                status = exp.get("status")
                if status is None:
                    raise ValueError(f"Missing expect.status for step '{label}'")
                content_type = _normalize_content_type(exp.get("content_type") or "")
                has_fields = exp.get("has_fields") or []
                field_types = exp.get("field_types") or {}
                nested_validations = exp.get("nested_validations") or {}
                matches = exp.get("matches") or []
                captures = st.get("capture") or []

                # Optional IfController for dynamic enabled like "${ALLOW_DESTRUCTIVE}"
                container_tree = block_tree
                if isinstance(enabled, str) and enabled.strip().startswith("${") and enabled.strip().endswith("}"):
                    varname = enabled.strip()[2:-1]
                    ic = create_if_controller(f"IF {varname} == true", varname)
                    block_tree.append(ic)
                    ic_tree = _el("hashTree")
                    block_tree.append(ic_tree)
                    container_tree = ic_tree
                elif isinstance(enabled, bool) and not enabled:
                    # Static disabled: wrap in IfController false (cleaner than element enabled=false)
                    ic = create_if_controller("DISABLED", "__DISABLED__")
                    # Create a variable that is always false? We'll just set condition to false.
                    _prop(ic, "stringProp", "IfController.condition", "false")
                    container_tree.append(ic)
                    ic_tree = _el("hashTree")
                    container_tree.append(ic_tree)
                    container_tree = ic_tree

                # Per-step local headers:
                # Stratégie: TOUJOURS créer un HeaderManager (Local) pour chaque step
                # en copiant les default_headers et en les surchargeant avec les headers spécifiques
                # IMPORTANT: Le HeaderManager doit être placé DANS le hashTree du sampler (enfant), pas avant (frère)
                local_headers: Dict[str, str] = {}
                
                # Commencer avec une copie des headers globaux (sauf Authorization)
                for k, v in (default_headers or {}).items():
                    if k.lower() != "authorization":
                        local_headers[str(k)] = str(v)

                # Surcharger avec les headers spécifiques au step
                # IMPORTANT: si la valeur est une chaîne vide, on SUPPRIME le header au lieu de le mettre à vide
                # (car JMeter ignore les valeurs vides et utilise l'héritage)
                for k, v in (headers or {}).items():
                    if v is None:
                        continue
                    if v == "":
                        # Supprimer le header pour éviter l'héritage
                        local_headers.pop(str(k), None)
                    else:
                        local_headers[str(k)] = str(v)

                # Sampler (créer d'abord le sampler)
                sampler = create_http_sampler(label, method, path, query, body)
                container_tree.append(sampler)
                sampler_tree = _el("hashTree")
                container_tree.append(sampler_tree)
                stats["samplers"] += 1

                # HeaderManager (Local) - DOIT être enfant du sampler (dans sampler_tree)
                if local_headers:
                    sampler_tree.append(create_local_header_manager("HTTP Headers (Local)", local_headers))
                    sampler_tree.append(_el("hashTree"))

                # Assertions
                add_response_code_assertion(sampler_tree, str(status))
                if content_type:
                    add_content_type_assertion(sampler_tree, content_type)
                if isinstance(has_fields, list) and has_fields:
                    add_jsr223_assert_has_fields(sampler_tree, has_fields)
                if isinstance(field_types, dict) and field_types:
                    add_jsr223_assert_field_types(sampler_tree, field_types)
                if isinstance(nested_validations, dict) and nested_validations:
                    add_jsr223_assert_nested_validations(sampler_tree, nested_validations)
                if isinstance(matches, list) and matches:
                    add_jsr223_assert_matches(sampler_tree, matches)

                # Capture (post-processor)
                if isinstance(captures, list) and captures:
                    add_jsr223_postprocessor_capture(sampler_tree, captures)

    return root, stats


def main() -> int:
    if yaml is None:
        print("❌ PyYAML manquant. Installe-le : pip install pyyaml", file=sys.stderr)
        return 1

    ap = argparse.ArgumentParser(description="Generate JMX from stateful contract YAML")
    ap.add_argument("--yml", required=False, default="out/api_contract_generated.yml", help="Path to contract-test-definition.yml (default: out/api_contract_generated.yml)")
    ap.add_argument("--out", required=False, help="Output .jmx path or directory. If omitted, output will be placed next to the input YAML with the same base name and .jmx extension")
    args = ap.parse_args()

    yml_path = Path(args.yml)
    if not yml_path.exists():
        print(f"❌ YAML not found: {yml_path}", file=sys.stderr)
        return 1

    with open(yml_path, "r", encoding="utf-8") as f:
        doc = yaml.safe_load(f)

    root, stats = build_jmx_from_yaml(doc)
    out_bytes = _pretty_xml_bytes(root)

    # Determine output path:
    # - If --out omitted -> same directory as YAML, same stem, .jmx extension
    # - If --out is an existing directory -> place file inside that dir with YAML stem
    # - If --out ends with a path separator or has no suffix -> treat as directory
    # - Otherwise treat --out as a file path
    if args.out:
        candidate = Path(args.out)
        # If explicit directory exists
        if candidate.exists() and candidate.is_dir():
            out_path = candidate / (yml_path.stem + ".jmx")
        else:
            # Trailing separator indicates directory even if not existing yet
            s = str(args.out)
            if s.endswith("/") or s.endswith("\\") or candidate.suffix == "":
                out_path = candidate / (yml_path.stem + ".jmx")
            else:
                out_path = candidate
    else:
        out_path = yml_path.with_suffix('.jmx')

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(out_bytes)

    print("✅ JMX generated:", out_path)
    print(f"   sequences={stats['sequences']} steps={stats['steps']} samplers={stats['samplers']}")
    print("ℹ️ Notes:")
    print("   - capture/matches supporte JSONPath simple: $.a.b.c (pas de tableaux).")
    print("   - enabled: \"${VAR}\" => IfController (VAR doit valoir 'true' pour exécuter).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
