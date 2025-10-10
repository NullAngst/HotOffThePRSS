#!/bin/bash

# This script intelligently converts an old config.json to the new format,
# merging any duplicate RSS feed URLs into a single entry with multiple webhooks.

INPUT_FILE="config.json"
OUTPUT_FILE="config_merged.json"

# Check if jq is installed
if ! command -v jq &> /dev/null
then
    echo "'jq' is not installed. Please install it to run this script."
    echo "On Debian/Ubuntu, run: sudo apt install jq"
    exit 1
fi

# Check if the input file exists
if [ ! -f "$INPUT_FILE" ]; then
    echo "Error: $INPUT_FILE not found in the current directory."
    exit 1
fi

echo "Reading $INPUT_FILE and merging duplicate feeds..."

# Use jq to perform the conversion and merge
# 1. Access the .FEEDS array.
# 2. Group all feed objects by their ".url" value. This creates an array of groups.
# 3. Map over each group to create a single, merged feed object.
# 4. For the merged object, take the common properties (id, url, etc.) from the first item in the group.
# 5. Create the new "webhooks" array by mapping over every item in the group,
#    collecting its webhook_url and name into the new format.
# 6. Finally, wrap the result back into the top-level { "FEEDS": [...] } structure.
jq '{
  FEEDS: (
    .FEEDS | group_by(.url) | map({
      id: .[0].id,
      name: .[0].name,
      url: .[0].url,
      update_interval: .[0].update_interval,
      webhooks: map({
        url: .webhook_url,
        label: (.name // "No Label")
      })
    })
  )
}' "$INPUT_FILE" > "$OUTPUT_FILE"

echo "Conversion complete!"
echo "New, merged configuration has been saved to $OUTPUT_FILE."
echo ""
echo "IMPORTANT: Please review $OUTPUT_FILE to ensure it looks correct."
echo "Once you have confirmed, you can replace your old config with the new one by running:"
echo "mv $OUTPUT_FILE config.json"
