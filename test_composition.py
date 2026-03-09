"""Tests unitaires pour la gestion des compositions (allOf/oneOf/anyOf)"""
import copy
import sys
sys.path.insert(0, '.')
from generate_test_cases_v3 import ContractExpert

def test_required_semantics():
    print("=" * 60)
    print("TEST 1: oneOf/anyOf required = intersection")
    print("=" * 60)
    expert = ContractExpert({"definitions": {}})

    # oneOf: branch1 requires [a,b], branch2 requires [b,c] => intersection = [b]
    schema_oneOf = {
        "oneOf": [
            {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}}, "required": ["a", "b"]},
            {"type": "object", "properties": {"b": {"type": "string"}, "c": {"type": "integer"}}, "required": ["b", "c"]}
        ]
    }
    reqs = expert._collect_required_from_composed(schema_oneOf)
    assert set(reqs) == {"b"}, f"FAIL oneOf: expected [b], got {reqs}"
    print(f"  oneOf required => {sorted(reqs)} (intersection) OK")

    # anyOf: branch1 requires [x], branch2 requires [y] => intersection = []
    schema_anyOf = {
        "anyOf": [
            {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
            {"type": "object", "properties": {"y": {"type": "string"}}, "required": ["y"]}
        ]
    }
    reqs = expert._collect_required_from_composed(schema_anyOf)
    assert reqs == [], f"FAIL anyOf: expected [], got {reqs}"
    print(f"  anyOf required => {reqs} (no common required) OK")

    # allOf: still union
    schema_allOf = {
        "allOf": [
            {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]},
            {"type": "object", "properties": {"b": {"type": "integer"}}, "required": ["b"]}
        ]
    }
    reqs = expert._collect_required_from_composed(schema_allOf)
    assert set(reqs) == {"a", "b"}, f"FAIL allOf: expected [a,b], got {reqs}"
    print(f"  allOf required => {sorted(reqs)} (union) OK")
    return schema_allOf, schema_oneOf


def test_flatten(schema_allOf, schema_oneOf):
    print()
    print("=" * 60)
    print("TEST 2: _flatten_schema_to_object")
    print("=" * 60)
    expert = ContractExpert({"definitions": {}})

    rules_allOf = expert.extract_exhaustive_schema(schema_allOf)
    flat = expert._flatten_schema_to_object(rules_allOf)
    assert set(flat["properties"].keys()) == {"a", "b"}, f"FAIL: props={list(flat['properties'].keys())}"
    assert set(flat["required"]) == {"a", "b"}, f"FAIL: required={flat['required']}"
    print(f"  allOf flat: props={sorted(flat['properties'].keys())}, required={sorted(flat['required'])} OK")

    rules_oneOf = expert.extract_exhaustive_schema(schema_oneOf)
    flat_one = expert._flatten_schema_to_object(rules_oneOf)
    assert set(flat_one["properties"].keys()) == {"a", "b", "c"}, f"FAIL: props={list(flat_one['properties'].keys())}"
    assert set(flat_one["required"]) == {"b"}, f"FAIL: required={flat_one['required']}"
    print(f"  oneOf flat: props={sorted(flat_one['properties'].keys())}, required={sorted(flat_one['required'])} OK")

    return rules_allOf, flat


def test_inline_properties():
    print()
    print("=" * 60)
    print("TEST 3: Inline properties alongside allOf preserved")
    print("=" * 60)
    expert = ContractExpert({"definitions": {}})
    schema = {
        "allOf": [
            {"type": "object", "properties": {"inner": {"type": "string"}}, "required": ["inner"]}
        ],
        "properties": {"outer": {"type": "integer"}},
        "required": ["outer"]
    }
    rules = expert.extract_exhaustive_schema(schema)
    flat = expert._flatten_schema_to_object(rules)
    assert "inner" in flat["properties"], f"FAIL: inner missing. props={list(flat['properties'].keys())}"
    assert "outer" in flat["properties"], f"FAIL: outer missing. props={list(flat['properties'].keys())}"
    print(f"  Inline + allOf flat: props={sorted(flat['properties'].keys())} OK")


def test_body_generation(rules_allOf, flat):
    print()
    print("=" * 60)
    print("TEST 4: Body generation from composed schema")
    print("=" * 60)
    expert = ContractExpert({"definitions": {}})

    example = expert.generate_smart_example(rules_allOf)
    assert isinstance(example, dict), f"FAIL: not dict: {example}"
    assert "a" in example and "b" in example, f"FAIL: missing keys: {example}"
    print(f"  allOf example => {example} OK")

    # Flat with required=[] (endpoint optionality)
    flat_empty = copy.deepcopy(flat)
    flat_empty["required"] = []
    example_empty = expert.generate_smart_example(flat_empty)
    assert example_empty == {}, f"FAIL: expected empty body, got {example_empty}"
    print(f"  Flat with required=[] => {example_empty} OK (respects endpoint optionality)")

    # Flat with all required (for negative tests)
    flat_full = copy.deepcopy(flat)
    flat_full["required"] = list(flat["properties"].keys())
    example_full = expert.generate_smart_example(flat_full)
    assert len(example_full) == len(flat["properties"]), f"FAIL: expected all keys, got {list(example_full.keys())}"
    print(f"  Flat with all required => {example_full} OK (full body for neg tests)")


def test_negative_tests_on_composition():
    print()
    print("=" * 60)
    print("TEST 5: Negative tests generated for composed schema")
    print("=" * 60)

    schema_allOf = {
        "allOf": [
            {"type": "object", "properties": {"name": {"type": "string", "minLength": 1, "maxLength": 50}}, "required": ["name"]},
            {"type": "object", "properties": {"age": {"type": "integer", "minimum": 0, "maximum": 150}}, "required": ["age"]}
        ]
    }
    swagger = {
        "paths": {
            "/test": {
                "post": {
                    "operationId": "createTest",
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": schema_allOf}}
                    },
                    "responses": {"200": {"description": "OK"}}
                }
            }
        },
        "definitions": {}
    }
    expert = ContractExpert(swagger)
    seq = expert.generate_sequence("/test", "post", swagger["paths"]["/test"]["post"])
    labels = [s["label"] for s in seq["steps"]]
    print(f"  Steps generated: {len(labels)}")
    for label in labels:
        print(f"    - {label}")

    missing = [l for l in labels if "Missing Body Field" in l]
    mismatch = [l for l in labels if "Type Mismatch" in l]
    boundary = [l for l in labels if "Below Minimum" in l or "Above Maximum" in l or "Exceeds MaxLength" in l]
    empty = [l for l in labels if "Empty Body" in l]

    print(f"\n  Missing field tests: {len(missing)}")
    print(f"  Type mismatch tests: {len(mismatch)}")
    print(f"  Boundary tests: {len(boundary)}")
    print(f"  Empty body tests: {len(empty)}")

    assert len(missing) >= 2, f"FAIL: expected >=2 missing-field tests for name,age. Got {len(missing)}"
    assert len(mismatch) >= 1, f"FAIL: expected >=1 type-mismatch test for integer age. Got {len(mismatch)}"
    assert len(boundary) >= 2, f"FAIL: expected >=2 boundary tests (min/max age, maxLength name). Got {len(boundary)}"
    assert len(empty) >= 1, f"FAIL: expected >=1 empty body test. Got {len(empty)}"
    print("  ALL ASSERTIONS PASSED")


def test_optional_body_composition():
    print()
    print("=" * 60)
    print("TEST 6: Optional body + composition (requestBody.required=false)")
    print("=" * 60)
    schema = {
        "allOf": [
            {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
            {"type": "object", "properties": {"y": {"type": "integer"}}, "required": ["y"]}
        ]
    }
    swagger = {
        "paths": {
            "/opt": {
                "put": {
                    "operationId": "updateOpt",
                    "requestBody": {
                        "required": False,
                        "content": {"application/json": {"schema": schema}}
                    },
                    "responses": {"200": {"description": "OK"}}
                }
            }
        },
        "definitions": {}
    }
    expert = ContractExpert(swagger)
    seq = expert.generate_sequence("/opt", "put", swagger["paths"]["/opt"]["put"])
    labels = [s["label"] for s in seq["steps"]]
    print(f"  Steps generated: {len(labels)}")
    for label in labels:
        print(f"    - {label}")

    missing = [l for l in labels if "Missing Body Field" in l]
    empty = [l for l in labels if "Empty Body" in l]

    print(f"\n  Missing field tests: {len(missing)} (expected 0: body is optional)")
    print(f"  Empty body tests: {len(empty)} (expected 0: body is optional)")

    assert len(missing) == 0, f"FAIL: requestBody.required=false => no missing-field tests. Got {len(missing)}"
    assert len(empty) == 0, f"FAIL: requestBody.required=false => no empty-body test. Got {len(empty)}"

    # But type mismatch / boundary tests should still exist (testing invalid values IF body is sent)
    mismatch = [l for l in labels if "Type Mismatch" in l]
    print(f"  Type mismatch tests: {len(mismatch)} (expected >=1: invalid body should still be tested)")
    assert len(mismatch) >= 1, f"FAIL: expected type-mismatch tests even for optional body. Got {len(mismatch)}"
    print("  ALL ASSERTIONS PASSED")


if __name__ == "__main__":
    schema_allOf, schema_oneOf = test_required_semantics()
    rules_allOf, flat = test_flatten(schema_allOf, schema_oneOf)
    test_inline_properties()
    test_body_generation(rules_allOf, flat)
    test_negative_tests_on_composition()
    test_optional_body_composition()
    print()
    print("=" * 60)
    print("ALL COMPOSITION TESTS PASSED")
    print("=" * 60)
