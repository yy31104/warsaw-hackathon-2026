from alerting.delivery.render import mask_external, mask_value


def test_masks_flattened_declared_paths_across_fields_aliases_and_rendered_text():
    fields = {
        "book.account": 876543211234,
        "$service": "prefix 876543211234",
        "book.notes.0": "internal-abcde",
        "book.notes.1": "no",
        "public": "visible",
    }
    rendered = {
        "title": "Account 876543211234 for prefix 876543211234",
        "body": "internal-abcde / no",
        "fields": fields,
        "color": "red",
    }
    masked = mask_external(rendered, fields, ["book.account", "book.notes"])
    assert masked["title"] == "Account ****1234 for prefix ****1234"
    assert masked["body"] == "****bcde / ****"
    assert masked["fields"] == {
        "book.account": "****1234", "$service": "prefix ****1234",
        "book.notes.0": "****bcde", "book.notes.1": "****", "public": "visible",
    }
    assert fields["book.account"] == 876543211234


def test_nested_container_masking_with_literals_and_overlapping_values():
    fields = {"private": {"values": ["a.b1234", "a.b", None]}, "count": 5}
    assert mask_external("a.b1234 axb1234 a.b", fields, ["private"]) == "****1234 axb1234 ****"
    assert mask_external(fields, fields, ["private"]) == {
        "private": {"values": ["****1234", "****", None]}, "count": 5,
    }
    assert mask_external({"private": 704}, {}, ["private"]) == {"private": "****"}
    assert mask_external({"private.ref": 123456789}, {}, ["private"]) == {"private.ref": "****6789"}
    assert mask_external(fields, fields, []) == fields
    assert mask_value("1234") == "****"
