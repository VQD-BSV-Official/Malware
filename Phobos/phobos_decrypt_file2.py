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
import struct
import base64
from binascii import crc32
from Crypto.Cipher import AES


# Metadata
METADATA_SIZE = 0xB2

AES_IV_POS = 0x14
AES_IV_SIZE = 16
PADDING_SIZE_POS = 0x24
ENC_KEY_DATA_POS = 0x28
RSA_KEY_SIZE = 128
FOOTER_SIZE_POS = 0xA8
ATTACKER_ID_POS = 0xAC
ATTACKER_ID_SIZE = 6

AES_KEY_SIZE = 32


KEY_ENTRY_SIZE = RSA_KEY_SIZE + AES_KEY_SIZE

KEY_DATA_XOR = 0x17F31AAB


# Encryption info
ENC_MODE_POS = 4
ORIG_FILENAME_POS_POS = 0x18

ENC_MODE1_MAGIC = 0xAF77BC0F
ENC_MODE2_MAGIC = 0xF0A75E12


ENC_BLOCK_SIZE = 0xD0000


def decrypt_file2(filepath: str, key_data: bytes) -> bool:
    """Decrypt file"""

    with io.open(filepath, 'rb') as f:

        # Read metadata
        try:
            f.seek(-METADATA_SIZE, 2)
        except OSError:
            return False

        metadata = f.read(METADATA_SIZE)

        # Attacker ID
        attacker_id = metadata[ATTACKER_ID_POS :
                               ATTACKER_ID_POS + ATTACKER_ID_SIZE]

        # Encrypted key data
        enc_key_data = metadata[ENC_KEY_DATA_POS :
                                ENC_KEY_DATA_POS + RSA_KEY_SIZE]

        # Find decryption key
        for i in range(0, len(key_data), KEY_ENTRY_SIZE):

            if (enc_key_data == key_data[i : i + RSA_KEY_SIZE]):
                aes_key = key_data[i + RSA_KEY_SIZE:
                                   i + RSA_KEY_SIZE + AES_KEY_SIZE]
                break

        else:
            return False

        # Footer size including metadata
        footer_size, = struct.unpack_from('<L', metadata, FOOTER_SIZE_POS)
        if footer_size <= METADATA_SIZE:
            return False

        # Read end block with encryption info
        endblock_size = footer_size - METADATA_SIZE
        if (endblock_size & 0xF) != 0:
            return False

        try:
            f.seek(-footer_size, 2)
        except OSError:
            return False
        
        enc_endblock_data = f.read(endblock_size)

    # AES IV
    aes_iv = metadata[AES_IV_POS : AES_IV_POS + AES_IV_SIZE]
    # Padding size
    padding_size, = struct.unpack_from('<L', metadata, PADDING_SIZE_POS)

    # Decrypt end block with encryption info
    cipher = AES.new(aes_key, AES.MODE_CBC, aes_iv)
    endblock_data = cipher.decrypt(enc_endblock_data)

    # Encryption mode
    enc_mode, enc_mode_magic = struct.unpack_from('<2L', endblock_data,
                                                  ENC_MODE_POS)
    if enc_mode == 1:
        if enc_mode_magic != ENC_MODE1_MAGIC:
            return False
    elif enc_mode == 2:
        if enc_mode_magic != ENC_MODE2_MAGIC:
            return False
    else:
        return False

    # Original file name
    orig_filename_pos, = struct.unpack_from('<L', endblock_data,
                                            ORIG_FILENAME_POS_POS)
    for i in range(orig_filename_pos, len(endblock_data), 2):
        if (endblock_data[i] == 0) and (endblock_data[i + 1] == 0):
            break
    else:
        return False
    orig_filename = endblock_data[orig_filename_pos : i].decode('UTF-16')
    i = orig_filename.rfind('\\')
    if i >= 0:
        orig_filename = orig_filename[i + 1:]

    new_filepath = os.path.join(os.path.dirname(filepath), orig_filename)

    # Decrypt file
    if enc_mode == 1:

        # Multiple chunks

        # Copy file with original name
        shutil.copy(filepath, new_filepath)

        with io.open(new_filepath, 'rb+') as f:

            num_chunks, chunk_size, chunk_data_crc32 = \
                struct.unpack_from('<3L', endblock_data, 0x0C)

            chunk_data_pos = 0x20 + num_chunks * 8
            chunk_data = endblock_data[chunk_data_pos :
                                       chunk_data_pos +
                                       num_chunks * chunk_size]

            # Check decrypted chunk data CRC32
            if chunk_data_crc32 != crc32(chunk_data):
                return False

            # Write decrypted chunk data
            for i in range(num_chunks):
                chunk_pos, = struct.unpack_from('<Q', endblock_data,
                                                0x20 + i * 8)
                f.seek(chunk_pos)
                f.write(chunk_data[i * chunk_size : (i + 1) * chunk_size])

            # Remove footer
            f.seek(-footer_size, 2)
            f.truncate()

    else:

        # Single chunk

        with io.open(filepath, 'rb') as fin:
            with io.open(new_filepath, 'wb') as fout:

                cipher = AES.new(aes_key, AES.MODE_CBC, aes_iv)

                while True:

                    enc_data = fin.read(ENC_BLOCK_SIZE)
                    if len(enc_data) != ENC_BLOCK_SIZE:
                        # Last encrypted block
                        excess_size = len(enc_data) & 0xF
                        dec_data = cipher.decrypt(enc_data[:-excess_size])
                        fout.write(dec_data + enc_data[-excess_size:])
                        break

                    dec_data = cipher.decrypt(enc_data)
                    fout.write(dec_data)

                # Remove footer
                fout.seek(-(footer_size + padding_size), 2)
                fout.truncate()

    return True


#
# Main
#
if len(sys.argv) != 2:
    print('Usage:', os.path.basename(sys.argv[0]), 'filename')
    sys.exit(0)

filename = sys.argv[1]

# Read decryption code (Phobos decryptor)
with io.open('./keys.txt', 'rb') as f:
    key_data = base64.b64decode(f.read())

num_keys, crc = struct.unpack_from('<2L', key_data, 0)
num_keys ^= KEY_DATA_XOR
crc ^= KEY_DATA_XOR
print('keys:', num_keys)
print('key data crc32: %08X' % crc)

key_data = key_data[8 : 8 + num_keys * KEY_ENTRY_SIZE]

if crc != crc32(key_data):
    print('Error: Invalid key data.')
    sys.exit(1)

# Decrypt file
if not decrypt_file2(filename, key_data):
    print('Error: Failed to decrypt file')
    sys.exit(1)
