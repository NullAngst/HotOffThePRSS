#!/bin/bash

# This script intelligently converts an old config.json to the new format,
# merging any duplicate RSS feed URLs into a single entry with multiple webhooks.
#
# Compared to earlier versions, this one preserves:
#   - the per-feed `active` flag (defaulting to true if missing)
#   - each webhook's own `label` when migrating from the flat-list format

INPUT_FILE="config.json"
OUTPUT_FILE="config_merged.json"

if ! command -v jq &> /dev/null; then
    echo "'jq' is not installed. Please install it to run this script."
    echo "On Debian/Ubuntu, run: sudo apt install jq"
    exit 1
fi

if [ ! -f "$INPUT_FILE" ]; then
    echo "Error: $INPUT_FILE not found in the current directory."
    exit 1
fi

echo "Reading $INPUT_FILE and merging duplicate feeds..."

# Group feed entries by URL, then build a single merged entry per URL.
# For each source entry we contribute either its already-structured webhooks
# (new format) or a single {url,label} synthesized from the legacy flat fields.
jq '{
  FEEDS: (
    .FEEDS | group_by(.url) | map(
      (.[0]) as $head
      | {
          id: ($head.id // (now | tostring)),
          name: ($head.name // $head.url),
          url: $head.url,
          update_interval: ($head.update_interval // 300),
          active: ([.[] | (.active // true)] | any),
          webhooks: (
            [
              .[] | (
                if (.webhooks | type) == "array" then
                  .webhooks[]
                elif (.webhook_urls | type) == "array" then
                  .webhook_urls[] | {url: ., label: ""}
                elif (.webhook_url | type) == "string" then
                  {url: .webhook_url, label: (.name // "")}
                else
                  empty
                end
              )
            ]
            # De-duplicate webhooks by URL, keeping the first non-empty label seen.
            | group_by(.url) | map({
                url: .[0].url,
                label: ([.[].label] | map(select(. != null and . != "")) | .[0] // "")
              })
          )
        }
    )
  )
}' "$INPUT_FILE" > "$OUTPUT_FILE"

echo "Conversion complete!"
echo "New, merged configuration has been saved to $OUTPUT_FILE."
echo ""
echo "IMPORTANT: Please review $OUTPUT_FILE to ensure it looks correct."
echo "Once you have confirmed, you can replace your old config with the new one by running:"
echo "mv $OUTPUT_FILE config.json"
