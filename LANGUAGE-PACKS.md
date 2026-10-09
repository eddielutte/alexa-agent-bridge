# Drafting a language pack

English works in every English-speaking country. For any other Alexa language, the owner's agent can draft a pack for the owner to check. A drafted pack is marked `untested` until a real spoken request has worked with it.

**Current limit:** the setup tool's own checks (`enrol` and `test`) talk to the skill in English. Setting up with a non-English pack is therefore untested and is expected to stop at `enrol`. Until that's fixed, use English where the country offers it.

A pack is two files:

| File | Contents |
| --- | --- |
| `lambda/lang_<code>.json` | `language`, `status` (`untested` for drafts), `carriers` and `strings` |
| `models/<code>.json` | `language`, `status` and `languageModel`, the voice model without its invocation name |

`<code>` is the two-letter language code, such as `de`, `fr` or `ja`.

## Steps for the agent

1. **Copy the English files** `lambda/lang_en.json` and `models/en.json` to the new names.
2. **Translate every value in `strings`.** Keep the keys, and keep every placeholder in braces (`{name}`, `{agent}`, `{device}`, `{guidance}`) exactly. Write the replies as natural, short spoken sentences, not literal translations.
3. **Translate the model's sample phrases.**
   - Each generic intent has one sample that ends in `{request}`, such as `"tell me {request}"`. Translate the opening words, keep `{request}` at the end, and keep the intent names unchanged.
   - Put the same opening words, followed by a space, into `carriers` under the same intent name. `PleaseIntent` keeps an empty carrier.
   - The Echo-naming, linking, status, renewal, setup and connection-test intents also need natural phrases in the new language.
   - Built-in `AMAZON.*` intents need no samples.
4. **Set `"status": "untested"`** in both files.
5. **Run `python3 -m bridge pack-check --language <code>`** and fix everything it reports: missing strings, changed placeholders, and carriers without a matching model sample.
6. **Show the owner the translated phrases** (the samples and the strings, as a simple two-column list) and ask them to correct anything that sounds wrong. A native speaker's check matters more than anything the tool can test.
7. **Use the pack:** `python3 -m bridge choose --language <code> ...`, then continue setup as normal.
8. **Promote the pack.** After the owner hears a correct answer to a real request, set `"status": "proven"`. With the owner's permission, offer the pack to the project as a pull request.

## Invocation names in other languages

`build_model.py` fully checks English names. For other languages it only checks the universal rules: lower case, at least two words, no digits, no wake words such as “alexa”, and acronyms as single letters with full stops. Look up Amazon's invocation-name guidance for that language. If the owner's chosen name is unusual, test it in the simulator before relying on it.
