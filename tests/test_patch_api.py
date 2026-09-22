"""End-to-end tests for POST /patch (RFC 6902 style add/remove/replace)."""

import copy

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _patch(document, operations):
    return client.post(
        "/patch", json={"document": document, "operations": operations}
    )


DOC = {
    "subject": {"name": "Ada", "age": 36, "nickname": None},
    "emails": ["ada@example.com", "ada@analytical.engine"],
    "emptyObj": {},
    "emptyArr": [],
}


# ---------------------------------------------------------------------------
# Success cases
# ---------------------------------------------------------------------------


def test_patch_object_add_replace_and_remove():
    doc = copy.deepcopy(DOC)
    resp = _patch(
        doc,
        [
            {"op": "add", "path": "/subject/role", "value": "engineer"},
            {"op": "replace", "path": "/subject/age", "value": 37},
            {"op": "add", "path": "/subject/nickname", "value": "Countess"},
            {"op": "remove", "path": "/emails/0"},
        ],
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["document"] == {
        "subject": {
            "name": "Ada",
            "age": 37,
            "nickname": "Countess",
            "role": "engineer",
        },
        "emails": ["ada@analytical.engine"],
        "emptyObj": {},
        "emptyArr": [],
    }
    assert data["changedPaths"] == [
        "/emails/0",
        "/subject/age",
        "/subject/nickname",
        "/subject/role",
    ]
    # The submitted document is never mutated.
    assert doc == DOC


def test_patch_array_insert_indices_and_append_dash():
    resp = _patch(
        {"xs": [1, 2, 3]},
        [
            {"op": "add", "path": "/xs/0", "value": 0},
            {"op": "add", "path": "/xs/-", "value": 4},
            {"op": "add", "path": "/xs/2", "value": "mid"},
        ],
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["document"]["xs"] == [0, 1, "mid", 2, 3, 4]
    # The '-' append happened while the array had four elements (after the
    # two earlier inserts), so it resolved to index 4; changedPaths are RFC
    # 6901 sorted, not in application order.
    assert data["changedPaths"] == ["/xs/0", "/xs/2", "/xs/4"]


def test_patch_dash_resolves_against_current_document_each_step():
    # Two appends in a row must resolve to growing indices.
    resp = _patch(
        {"xs": []},
        [
            {"op": "add", "path": "/xs/-", "value": "a"},
            {"op": "add", "path": "/xs/-", "value": "b"},
        ],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["document"] == {"xs": ["a", "b"]}
    assert body["changedPaths"] == ["/xs/0", "/xs/1"]


def test_patch_replace_array_element_and_object_member_overwrite():
    resp = _patch(
        {"a": {"b": [1, 2, 3]}, "name": "x"},
        [
            {"op": "replace", "path": "/a/b/1", "value": 9},
            {"op": "replace", "path": "/name", "value": [1, 2]},
        ],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["document"] == {"a": {"b": [1, 9, 3]}, "name": [1, 2]}
    assert body["changedPaths"] == ["/a/b/1", "/name"]


def test_patch_add_then_use_the_new_location_in_a_later_op():
    # Operations execute in order against the evolving copy.
    resp = _patch(
        {"a": 1},
        [
            {"op": "add", "path": "/b", "value": {"c": []}},
            {"op": "add", "path": "/b/c/-", "value": "deep"},
            {"op": "replace", "path": "/b/c/0", "value": "deeper"},
        ],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["document"] == {"a": 1, "b": {"c": ["deeper"]}}
    assert body["changedPaths"] == ["/b", "/b/c/0"]


def test_patch_remove_from_empty_containers_and_nested_values():
    resp = _patch(
        copy.deepcopy(DOC),
        [
            {"op": "remove", "path": "/emptyObj"},
            {"op": "remove", "path": "/subject/nickname"},
            {"op": "replace", "path": "/emptyArr", "value": []},
        ],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "emptyObj" not in body["document"]
    assert "nickname" not in body["document"]["subject"]
    assert body["changedPaths"] == ["/emptyArr", "/emptyObj", "/subject/nickname"]


def test_patch_empty_operations_is_a_noop():
    doc = copy.deepcopy(DOC)
    resp = _patch(doc, [])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["document"] == DOC
    assert body["changedPaths"] == []
    # The "patched" document is a copy, not the same object.
    assert body["document"] is not doc
    assert doc == DOC


def test_patch_root_operations():
    # add/replace at "" replaces the whole document.
    resp = _patch(42, [{"op": "replace", "path": "", "value": {"a": 1}}])
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"document": {"a": 1}, "changedPaths": [""]}

    resp = _patch([1], [{"op": "add", "path": "", "value": "rooted"}])
    assert resp.status_code == 200
    assert resp.json()["document"] == "rooted"


def test_patch_supports_escaped_member_names():
    resp = _patch(
        {"a/b": 1, "tilde~key": 2},
        [
            {"op": "replace", "path": "/a~1b", "value": 10},
            {"op": "remove", "path": "/tilde~0key"},
        ],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["document"] == {"a/b": 10}
    assert resp.json()["changedPaths"] == ["/a~1b", "/tilde~0key"]


def test_patch_accepts_member_named_dash_on_objects():
    # '-' is special only for arrays; for objects it is an ordinary key.
    resp = _patch({}, [{"op": "add", "path": "/-", "value": 1}])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["document"] == {"-": 1}
    assert body["changedPaths"] == ["/-"]


def test_patch_add_container_value_deep_copied_into_document():
    value = {"nested": [1, {"x": 2}]}
    resp = _patch({}, [{"op": "add", "path": "/v", "value": value}])
    assert resp.status_code == 200, resp.text
    assert resp.json()["document"] == {"v": {"nested": [1, {"x": 2}]}}


# ---------------------------------------------------------------------------
# Validation / atomicity failures
# ---------------------------------------------------------------------------


def _expect_operations_error(resp, index):
    assert resp.status_code == 400, resp.text
    err = resp.json()["error"]
    assert err["loc"] == f"operations[{index}]", err
    return err


def test_patch_failure_in_later_op_locates_it_and_returns_no_partial_result():
    doc = copy.deepcopy(DOC)
    resp = _patch(
        doc,
        [
            {"op": "replace", "path": "/subject/age", "value": 37},
            {"op": "remove", "path": "/subject/missing"},
        ],
    )
    err = _expect_operations_error(resp, 1)
    assert err["code"] == "PATH_NOT_FOUND"
    assert "missing" in err["message"]
    # No patched document, no changedPaths: the response is only the error.
    assert set(resp.json().keys()) == {"error"}
    # The caller's document is untouched.
    assert doc == DOC


def test_patch_remove_missing_object_member():
    err = _expect_operations_error(
        _patch({"a": 1}, [{"op": "remove", "path": "/b"}]), 0
    )
    assert err["code"] == "PATH_NOT_FOUND"


def test_patch_replace_missing_object_member():
    err = _expect_operations_error(
        _patch({"a": 1}, [{"op": "replace", "path": "/b", "value": 2}]), 0
    )
    assert err["code"] == "PATH_NOT_FOUND"


def test_patch_array_index_out_of_range_for_remove_and_replace():
    err = _expect_operations_error(
        _patch({"xs": [1, 2]}, [{"op": "remove", "path": "/xs/2"}]), 0
    )
    assert err["code"] == "PATH_NOT_FOUND"
    assert err["detail"]["arrayLength"] == 2

    err = _expect_operations_error(
        _patch({"xs": [1, 2]}, [{"op": "replace", "path": "/xs/9", "value": 0}]),
        0,
    )
    assert err["code"] == "PATH_NOT_FOUND"


def test_patch_add_array_index_past_end_is_invalid_but_dash_appends():
    # Add may insert at length (== '-'), but not beyond it.
    err = _expect_operations_error(
        _patch({"xs": [1]}, [{"op": "add", "path": "/xs/5", "value": 2}]), 0
    )
    assert err["code"] == "PATH_NOT_FOUND"

    assert _patch(
        {"xs": [1]}, [{"op": "add", "path": "/xs/1", "value": 2}]
    ).status_code == 200


def test_patch_remove_and_replace_with_dash_are_invalid():
    err = _expect_operations_error(
        _patch({"xs": [1]}, [{"op": "remove", "path": "/xs/-"}]), 0
    )
    assert err["code"] == "PATH_NOT_FOUND"

    err = _expect_operations_error(
        _patch({"xs": [1]}, [{"op": "replace", "path": "/xs/-", "value": 2}]), 0
    )
    assert err["code"] == "PATH_NOT_FOUND"


def test_patch_leading_zero_and_non_numeric_tokens_are_not_array_indices():
    err = _expect_operations_error(
        _patch({"xs": [1]}, [{"op": "remove", "path": "/xs/01"}]), 0
    )
    assert err["code"] == "PATH_NOT_FOUND"

    err = _expect_operations_error(
        _patch({"xs": [1]}, [{"op": "replace", "path": "/xs/x", "value": 0}]), 0
    )
    assert err["code"] == "PATH_NOT_FOUND"


def test_patch_parent_is_a_scalar():
    err = _expect_operations_error(
        _patch({"a": 1}, [{"op": "add", "path": "/a/b", "value": 2}]), 0
    )
    assert err["code"] == "PATH_NOT_FOUND"
    assert "scalar" in err["message"]


def test_patch_missing_parent_container():
    err = _expect_operations_error(
        _patch({"a": {}}, [{"op": "add", "path": "/a/b/c", "value": 1}]), 0
    )
    assert err["code"] == "PATH_NOT_FOUND"


def test_patch_cannot_remove_the_root_document():
    err = _expect_operations_error(
        _patch({"a": 1}, [{"op": "remove", "path": ""}]), 0
    )
    assert err["code"] == "PATH_NOT_FOUND"


def test_patch_pointer_syntax_error_is_located():
    resp = _patch({"a": 1}, [{"op": "remove", "path": "a"}])
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "POINTER_SYNTAX"
    assert err["loc"] == "operations[0].path"

    resp = _patch({"a": 1}, [{"op": "remove", "path": "/a~2"}])
    assert resp.status_code == 400
    assert resp.json()["error"]["loc"] == "operations[0].path"


def test_patch_operation_shape_errors():
    # operations is not an array
    resp = _patch({}, "nope")
    assert resp.status_code == 400
    assert resp.json()["error"]["loc"] == "operations"

    # entry is not an object
    resp = _patch({}, ["nope"])
    assert resp.status_code == 400
    assert resp.json()["error"]["loc"] == "operations[0]"

    # missing op
    resp = _patch({}, [{"path": "/a"}])
    assert resp.status_code == 400
    assert resp.json()["error"]["loc"] == "operations[0].op"

    # unsupported op
    resp = _patch({}, [{"op": "move", "path": "/a", "from": "/b"}])
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["loc"] == "operations[0].op"
    assert err["detail"]["supported"] == ["add", "remove", "replace"]

    # missing / non-string path
    resp = _patch({}, [{"op": "remove"}])
    assert resp.status_code == 400
    assert resp.json()["error"]["loc"] == "operations[0].path"

    resp = _patch({}, [{"op": "remove", "path": 3}])
    assert resp.status_code == 400
    assert resp.json()["error"]["loc"] == "operations[0].path"

    # add/replace require value; remove forbids it
    resp = _patch({}, [{"op": "add", "path": "/a"}])
    assert resp.status_code == 400
    assert resp.json()["error"]["loc"] == "operations[0].value"

    resp = _patch({"a": 1}, [{"op": "remove", "path": "/a", "value": 1}])
    assert resp.status_code == 400
    assert resp.json()["error"]["loc"] == "operations[0].value"


def test_patch_earlier_operations_are_validated_against_evolving_copy():
    # The path only exists after the first add; failure in the third op must
    # not report against the original document nor leak the first two steps.
    resp = _patch(
        {"xs": []},
        [
            {"op": "add", "path": "/xs/-", "value": {"y": 1}},
            {"op": "replace", "path": "/xs/0/y", "value": 2},
            {"op": "remove", "path": "/xs/0/z"},
        ],
    )
    err = _expect_operations_error(resp, 2)
    assert err["code"] == "PATH_NOT_FOUND"
    assert "document" not in resp.json()


def test_patch_static_validation_runs_before_any_application():
    # A syntactically bad operation later in the list fails even though all
    # earlier operations would apply cleanly.
    doc = {"a": 1}
    resp = _patch(
        doc,
        [
            {"op": "replace", "path": "/a", "value": 2},
            {"op": "bogus", "path": "/a"},
        ],
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["loc"] == "operations[1].op"
    assert doc == {"a": 1}


def test_patch_value_uses_existing_json_validation():
    # NaN cannot be serialized by a strict JSON client; send it as raw JSON,
    # the same way the existing validation contract is exercised on /verify.
    resp = client.post(
        "/patch",
        content=(
            b'{"document": {}, "operations": '
            b'[{"op": "add", "path": "/x", "value": NaN}]}'
        ),
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "UNSUPPORTED_VALUE"
    assert err["loc"] == "operations[0].value"


def test_patch_request_level_errors():
    # Not a JSON object body.
    resp = client.post("/patch", json=[1, 2])
    assert resp.status_code == 400
    assert resp.json()["error"]["loc"] == "body"

    # Missing fields.
    resp = client.post("/patch", json={"document": {}})
    assert resp.status_code == 400
    assert resp.json()["error"]["loc"] == "operations"

    resp = client.post("/patch", json={"operations": []})
    assert resp.status_code == 400
    assert resp.json()["error"]["loc"] == "document"


def test_patch_malformed_json_and_duplicate_keys_rejected():
    resp = client.post("/patch", content=b'{"document": {},', headers={})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_JSON"

    resp = client.post(
        "/patch",
        content=(
            b'{"document": {"a": 1, "a": 2}, '
            b'"operations": [{"op": "remove", "path": "/a"}]}'
        ),
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_JSON"


def test_patch_changed_paths_are_deduplicated_and_pointer_sorted():
    resp = _patch(
        {"a": 1, "b": [10, 20], "z": 0},
        [
            {"op": "replace", "path": "/a", "value": 2},
            {"op": "replace", "path": "/a", "value": 3},
            {"op": "replace", "path": "/b/0", "value": 11},
            {"op": "remove", "path": "/z"},
        ],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["changedPaths"] == ["/a", "/b/0", "/z"]
