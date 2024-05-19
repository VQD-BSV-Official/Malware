# MIT License
#
# Copyright (c) 2023-2024 Andrey Zhdanov (rivitna)
# https://github.com/rivitna
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to permit
# persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be included
# in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.

import sys
import io
import os
import shutil
import base64
from Crypto.Cipher import AES
import rcru64_crypt


RANSOM_EXT_PREFIX = '_[ID-'


# Markers
METADATA_MARKER = b'wenf='
METADATA_MARKER2 = b'&4r*3d'

CHUNK_INFO_MARKER1 = b'P7A1s'
CHUNK_INFO_MARKER2 = b':'
CHUNK_INFO_MARKER3 = b'$f1;'

ENC_SIZE_MARKER = b'Fs1z3'

KEYDATA_MARKER1 = b'd7j3'
KEYDATA_MARKER2 = b'y9a0'
KEYDATA_MARKER3 = b'm5ha'

SMALL_FILE_END_MARKER1 = b'nqpso5938fh71jfu'
SMALL_FILE_END_MARKER2 = b'qpso5938fh71jf'


# RSA
RSA_KEY_SIZE = 256

# AES GCM
AES_BLOCK_SIZE = 16

CHUNK_ENC_NONCE_SIZE = 12
FULL_ENC_NONCE_SIZE1 = 12
FULL_ENC_NONCE_SIZE2 = 32
FAST_ENC_NONCE_SIZE = 32

FAST_ENC_NONCE = None


# Metadata
MAX_METADATA_SIZE = 0x12C
MIN_METADATA_SIZE = RSA_KEY_SIZE + len(METADATA_MARKER)
ADDITIONAL_DATA_SIZE = 1


MIN_BIG_FILE_SIZE = 0x1F4000
ENC_BLOCK_SIZE = 0x7D000
FIRST_ENC_BLOCK_SIZE = 0x2D2A8


BLOCK_SIZE = 1024000


# 64-bit pseudo random number generator
RND64_INIT_STATE_DATA = b'sxuojgdg'
RND64_A1 = 4815
RND64_A2 = 4815


MASK64 = 0xFFFFFFFFFFFFFFFF

rol64 = lambda v, s: ((v << s) & MASK64) | ((v & MASK64) >> (64 - s))
ror64 = lambda v, s: (v >> s) | ((v << (64 - s)) & MASK64)


def rnd64_seed(state64: int, a1: int, a2: int) -> (int, int):
    """64-bit pseudo random number generator"""

    x1 = (state64 + 0xDEADBEEFDEADBEEF) & MASK64
    y1 = rol64(x1, 15)
    x2 = ((x1 ^ 0xE6ADBEEFDEADBEEF) + y1) & MASK64
    y2 = ror64(x2, 12)
    x3 = ((a1 ^ x2) + y2) & MASK64
    y3 = rol64(x3, 26)
    x4 = ((a2 ^ x3) + y3) & MASK64
    y4 = ror64(x4, 13)
    x5 = ((y1 ^ x4) + y4) & MASK64
    y5 = rol64(x5, 28)
    x6 = ((y2 ^ x5) + y5) & MASK64
    y6 = rol64(x6, 9)
    x7 = ((y3 ^ x6) + y6) & MASK64
    y7 = ror64(x7, 17)
    x8 = ((y4 ^ x7) + y7) & MASK64
    y8 = ror64(x8, 10)
    x9 = ((y5 ^ x8) + y8) & MASK64
    x10 = ((y6 ^ x9) + ror64(x9, 32)) & MASK64
    x11 = ((y7 ^ x10) + rol64(x10, 25)) & MASK64
    y12 = ror64(x11, 1)
    x12 = ((y8 ^ x11) + y12) & MASK64
    return y12, x12


def rnd64data(state64_data: bytes) -> bytes:
    """64-bit pseudo random data generator"""

    state64 = int.from_bytes(state64_data[:8], byteorder='little')
    rnd64, dummy = rnd64_seed(state64, RND64_A1, RND64_A2)
    return rnd64.to_bytes(8, byteorder='big')


def decrypt_file(filename: str, priv_key_data: bytes) -> bool:
    """Decrypt file"""

    with io.open(filename, 'rb+') as f:

        # Get file size
        f.seek(0, 2)
        file_size = f.tell()
        if file_size < len(METADATA_MARKER2):
            return False

        # Check and remove end marker
        f.seek(-len(METADATA_MARKER2), 2)
        marker = f.read(len(METADATA_MARKER2))
        if marker == METADATA_MARKER2:
            file_size -= len(METADATA_MARKER2)
            f.truncate(file_size)

        if file_size < MIN_METADATA_SIZE:
            return False

        # Read metadata
        size = min(file_size, MAX_METADATA_SIZE)
        f.seek(-size, 2)
        metadata = f.read(size)

        pos = metadata.find(METADATA_MARKER)
        if pos < 0:
            return False

        file_size -= size - pos

        print('metadata marker pos: %08X' % file_size)

        pos += len(METADATA_MARKER)
        metadata = metadata[pos:]

        pos = metadata.find(CHUNK_INFO_MARKER1)
        if pos >= 0:
            # Chunk encryption
            chunc_enc = True

            # Get encrypted key data
            enc_keydata = metadata[:pos]

            # Chunk size
            pos += len(CHUNK_INFO_MARKER1)
            pos2 = metadata.find(CHUNK_INFO_MARKER2, pos)
            if pos2 < 0:
                return False

            chunk_size = int(float(metadata[pos : pos2]) * BLOCK_SIZE)

            print('chunk size: %d' % chunk_size)

            # Chunk space
            pos = pos2 + len(CHUNK_INFO_MARKER2)
            pos2 = metadata.find(CHUNK_INFO_MARKER3, pos)
            if pos2 < 0:
                return False

            chunk_space = int(float(metadata[pos : pos2]) * BLOCK_SIZE)

            print('chunk space: %d' % chunk_space)

            # Chunk step
            chunk_step = chunk_size + chunk_space

            # Encryption size
            pos = pos2 + len(CHUNK_INFO_MARKER3)
            pos = metadata.find(ENC_SIZE_MARKER, pos)
            if pos < 0:
                return False

            pos += len(ENC_SIZE_MARKER)
            enc_size = int(metadata[pos:])

            print('encryption size: %d' % enc_size)

        else:
            # Full/fast encryption
            chunc_enc = False

            # Get encrypted key data
            enc_keydata = metadata

        # Decrypt key data
        keydata = rcru64_crypt.rsa_decrypt(enc_keydata, priv_key_data)
        if keydata is None:
            print('RSA private key: Failed')
            return False

        print('RSA private key: OK')

        # Parse key data
        pos = keydata.find(KEYDATA_MARKER1)
        if pos < 0:
            return False

        pos += len(KEYDATA_MARKER1)
        pos2 = keydata.find(KEYDATA_MARKER2, pos)
        if pos2 < 0:
            return False

        # Get encryption key
        key = keydata[pos : pos2]

        pos = pos2 + len(KEYDATA_MARKER2)
        pos2 = keydata.find(KEYDATA_MARKER3, pos)
        if pos2 < 0:
            return False

        # Get nonce
        nonce = keydata[pos : pos2]

        if not chunc_enc:

            # Full/fast encryption
            enc_size = file_size

            if file_size < MIN_BIG_FILE_SIZE:

                # Full encryption, small file
                end_markers = [SMALL_FILE_END_MARKER1]

                if file_size > ENC_BLOCK_SIZE:
                    # Full encryption, small file, some blocks
                    print('mode: full encryption, small file, some blocks')

                    enc_size -= ADDITIONAL_DATA_SIZE + 1

                    f.seek(0)
                    enc_data = f.read(min(enc_size, ENC_BLOCK_SIZE))

                    # Decrypt first block
                    cipher = AES.new(key, AES.MODE_GCM,
                                     nonce[:FULL_ENC_NONCE_SIZE1])
                    data = cipher.decrypt(enc_data)

                    f.seek(0)
                    f.write(data)

                    pos = ENC_BLOCK_SIZE

                    rnd64_state_data = RND64_INIT_STATE_DATA

                    while pos < enc_size:

                        f.seek(pos)
                        enc_data = f.read(min(enc_size - pos,
                                              ENC_BLOCK_SIZE))
                        if enc_data == b'':
                            break

                        # Decrypt chunk
                        rnd64_state_data = rnd64data(rnd64_state_data)
                        nonce = rnd64_state_data + rnd64_state_data[4:]
                        cipher = AES.new(key, AES.MODE_GCM, nonce)
                        data = cipher.decrypt(enc_data)

                        f.seek(pos)
                        f.write(data)

                        pos += ENC_BLOCK_SIZE

                else:
                    # Full encryption, small file, single block
                    print('mode: full encryption, small file, single block')

                    enc_size -= AES_BLOCK_SIZE

                    f.seek(0)
                    enc_data = f.read(enc_size)

                    # Decrypt block
                    cipher = AES.new(key, AES.MODE_GCM,
                                     nonce[:FULL_ENC_NONCE_SIZE2])
                    data = cipher.decrypt(enc_data)

                    f.seek(0)
                    f.write(data)

                    end_markers.append(SMALL_FILE_END_MARKER2)

                # Remove end marker in small file
                size = len(max(end_markers, key=len))
                f.seek(file_size - size)
                data = f.read(size)
                for end_marker in end_markers:
                    pos = data.find(end_marker)
                    if pos >= 0:
                        file_size -= size - pos
                        break

            else:
                # Fast encryption, big file
                print('mode: fast encryption, big file')

                f.seek(0)
                enc_data = f.read(min(enc_size,
                                      FIRST_ENC_BLOCK_SIZE - AES_BLOCK_SIZE))

                # Decrypt first block
                cipher = AES.new(key, AES.MODE_GCM,
                                 nonce[:FAST_ENC_NONCE_SIZE])
                data = cipher.decrypt(enc_data)

                f.seek(0)
                f.write(data)

                pos = enc_size - ENC_BLOCK_SIZE

                f.seek(pos)
                enc_data = f.read(min(enc_size,
                                      ENC_BLOCK_SIZE - AES_BLOCK_SIZE))

                # Decrypt last block
                cipher = AES.new(key, AES.MODE_GCM, FAST_ENC_NONCE)
                data = cipher.decrypt(enc_data)

                f.seek(pos)
                f.write(data)

        else:
            # Chunk encryption
            print('mode: chunk encryption')

            f.seek(0)
            enc_data = f.read(min(enc_size, chunk_size))

            # Decrypt first chunk
            cipher = AES.new(key, AES.MODE_GCM, nonce[:CHUNK_ENC_NONCE_SIZE])
            data = cipher.decrypt(enc_data)

            f.seek(0)
            f.write(data)

            # Decrypt next chunks
            pos = chunk_step

            rnd64_state_data = RND64_INIT_STATE_DATA

            while pos < enc_size:

                f.seek(pos)
                enc_data = f.read(min(enc_size - pos, chunk_size))
                if enc_data == b'':
                    break

                # Decrypt chunk
                rnd64_state_data = rnd64data(rnd64_state_data)
                nonce = rnd64_state_data + rnd64_state_data[4:]
                cipher = AES.new(key, AES.MODE_GCM, nonce)
                data = cipher.decrypt(enc_data)

                f.seek(pos)
                f.write(data)

                pos += chunk_step

        file_size -= ADDITIONAL_DATA_SIZE

        # Remove footer
        f.truncate(file_size)

    return True


#
# Main
#
if len(sys.argv) != 2:
    print('Usage:', os.path.basename(sys.argv[0]), 'filename')
    sys.exit(0)

filename = sys.argv[1]

# Load RSA private key
with io.open('./rsa_privkey0.txt', 'rb') as f:
    priv_key_data = base64.b64decode(f.read())

# Load Fast mode nonce
with io.open('./fast_nonce.bin', 'rb') as f:
    FAST_ENC_NONCE = f.read()

# Copy file
pos = filename.find(RANSOM_EXT_PREFIX)
if pos >= 0:
    new_filename = filename[:pos]
else:
    new_filename = filename + '.dec'
shutil.copy(filename, new_filename)

# Decrypt file
if not decrypt_file(new_filename, priv_key_data):
    print('Error: Failed to decrypt file')
    sys.exit(1)
