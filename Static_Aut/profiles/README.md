# App Profiles

App profiles separate site-specific configuration from the static automation code.
The executor should not know application names directly. It reads `STATIC_DEFAULT_APP_NAME`,
selects the matching profile, then loads locators for that profile.

## How Profile Selection Works

1. The profile registry reads `STATIC_DEFAULT_APP_NAME` from `.env`.
2. It searches `Static_Aut/profiles/definitions/*.json` for a profile whose `app_name` matches that value.
3. The matching profile becomes the active app.
4. Static tools use that app's locator file.
5. If locator healing runs, healed selectors are written under that app name.

If `STATIC_DEFAULT_APP_NAME` is empty or does not match a profile `app_name`, execution fails with a clear configuration error.

## Profile Format

Create one JSON file per web application:

```json
{
  "app_name": "sample_app",
  "locator_file": "sample_app_locators.json"
}
```

Fields:

- `app_name`: Stable app id. Must match the top-level key in the locator JSON.
- `locator_file`: Locator JSON file under `Static_Aut/locators/definitions`.

## Adding A New Web App

1. Add a profile file:

```text
Static_Aut/profiles/definitions/<app_name>.json
```

2. Add a locator file:

```text
Static_Aut/locators/definitions/<app_name>_locators.json
```

3. Use the same app name as the top-level key:

```json
{
  "sample_app": {
    "product_card": [".product"],
    "cart_icon": [".cart-icon"]
  }
}
```

4. Set `.env` to the profile app name:

```text
STATIC_DEFAULT_APP_NAME=sample_app
```

5. Run the Xray test. Missing locators can be healed by MCP and saved to:

```text
Static_Aut/locators/healed_locators.json
```

## What Belongs In Code Vs Profile

Keep in code:

- Generic static tool definitions.
- Generic executor behavior.
- LLM fallback routing.
- MCP locator healing flow.

Keep in profile or locator JSON:

- Application names.
- The configured default app name.
- CSS/text/role selectors.
- App-specific healed locators.

This keeps the framework reusable across many web applications instead of binding it
to a demo site.
