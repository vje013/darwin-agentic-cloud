# Hashing Coverage

### Overview
Hashing.py is responsible as the data pipeline for converting python objects into raw bytes and deterministically encoding them using SHA-256.

### Explanation
It works by first converting a python object to a standardized json-formatted string with alphabetical ordering and no white space for consistency. Then, it encodes the data into utf-8 byte data. Finally, it encodes using SHA-256 and returns the hex-encoded result.

### The Gap
There is no dedicated tests/test_hashing.py file and hashing.py is only called indirectly. Since the encoding system relies on serialization and proper formatting in order to be deterministic, test cases on those settings would be beneficial.

### Links
* **Source Module:** [hashing.py](../../darwin/agenticcloud/hashing.py)