TREASURY_ADDRESS = b"\xff" * 32
STORAGE_ADDRESS = b"\x00" * 32

# Payment lanes per storage record. Each lane pays one provider, and every
# storage fee is multiplied by this number.
PROVIDERS_PER_RECORD = 2
