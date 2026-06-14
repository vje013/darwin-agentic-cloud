# Signing Coverage

### Overview
Signing.py manages local key pairs (Ed25519) and handles both the signing and verification of Darwin Agentic Cloud attestations. The interface is meant to be substituted for production tools later on.

### Explanation
First, the file checks for the existence of a private key in the signing.pem file in the home directory (or a custom path by the DARWIN_STATE_DIR environment variable). If the file does not exist, it creates a new one and locks down the permissions.

Then, using the Signer class, it can create a more compact key id using hashing.py, sign data with the private key, and verify signatures using the public key.

### The Gap
There is no dedicated tests/test_signing.py file, and signing.py is only exercised indirectly. Since the file directly interacts with the operating system to create files and change file permissions, its behavior may change depending on the os and possibly break without test cases to monitor it. Also, if errors or changes were to go unchecked in the signature verification logic, false negatives or false positives may arise and compromise security.

### Links
* **Source Module:** [signing.py](../../darwin/agenticcloud/signing.py)