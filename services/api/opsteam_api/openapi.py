"""Write the API's OpenAPI document to the path given on the command line, as stable JSON."""

import json
import sys
from pathlib import Path

from opsteam_api.main import create_app

text = json.dumps(create_app().openapi(), indent=2, sort_keys=True) + "\n"
Path(sys.argv[1]).write_text(text, encoding="utf-8", newline="\n")
