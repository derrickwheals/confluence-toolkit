In **Confluence Data Center**, text surrounded by backticks:

```
`like this`
```

is rendered as **inline code**. In the storage format, Confluence does *not* store this as literal backtick characters. Instead, it converts it into structured XHTML such as:

```xml
<code>like this</code>
```

or sometimes:

```xml
<ac:structured-macro ac:name="code">...</ac:structured-macro>
```

Because of that:

* The REST API does **not** see literal backticks.
* Searching for backticks via CQL will not match anything.
* If you're querying the rendered view (`body.view`), inline code may be HTML-encoded.

---

## Why It Looks Like It’s Ignoring the Text

If you're:

* Searching page content via CQL
* Parsing `body.view`
* Or doing string matching on page content

Inline-code text might appear to be missing because:

* It’s wrapped in `<code>` tags.
* It may be HTML-encoded.
* It may be inside macro structures in `body.storage`.

---

## Workarounds

### ✅ 1. Use `body.storage` Instead of `body.view`

Request:

```http
GET /rest/api/content/{id}?expand=body.storage
```

You’ll receive the canonical storage format where inline code appears as:

```xml
<code>example</code>
```

Search for `<code>` elements instead of backticks.

---

### ✅ 2. Parse the XHTML Properly (Recommended)

Instead of string searching:

* Parse the `body.storage.value` as XML/XHTML.
* Extract `<code>` nodes.
* Work with their inner text.

This is the most reliable approach.

---

### ✅ 3. If You Control Page Creation

If you're programmatically creating pages and need searchable literal characters:

* Avoid relying on backticks.
* Use plain text or a custom marker format.
* Or store machine-readable metadata in page properties instead.

---

### ✅ 4. For CQL Searches

CQL searches operate on indexed content, not raw storage format.

Try searching for the actual code text:

```cql
text ~ "example"
```

Do not search for:

```cql
text ~ "`example`"
```

Backticks are not indexed.

---

## Important Distinction

| Format  | What You See  | What REST Returns                      |
| ------- | ------------- | -------------------------------------- |
| Editor  | `code`        | `<code>code</code>`                    |
| Storage | XML           | XML                                    |
| View    | Rendered HTML | `<code>code</code>` (possibly encoded) |

---

### Bottom Line

The REST API does not ignore backtick text — it transforms it into structured `<code>` elements.

