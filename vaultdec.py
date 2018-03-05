#!/usr/bin/python
# -*- coding: utf-8 -*-
#
# Copyright 2015, Francesco "dfirfpi" Picasso <francesco.picasso@gmail.com>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""TODO."""

import construct
import optparse
import os
import sys

from Crypto.Cipher import AES

try:
    from DPAPI.Core import blob
    from DPAPI.Core import masterkey
    from DPAPI.Core import registry
except ImportError:
    raise ImportError('Missing dpapick, install it or set PYTHONPATH.')

import vaultschema
import vaultstruct


def check_parameters(options, args):
    """Simple checks on the parameters set by the user."""
    if not options.masterkeydir:
        sys.exit('Cannot decrypt anything without master keys.')
    if options.system and options.security:
        return
    if not options.sid:
        sys.exit('You must provide user SID or os hives!')
    if not options.password and not options.pwdhash:
        sys.exit(
            'You must provide the user password or the user password hash. '
            'The user password hash is the SHA1(UTF_LE(password)), and must '
            'be provided as the hex textual string.')
    if not args or len(args) != 1:
        sys.exit('You must provide vaults directory.')
    elif not os.path.isdir(args[0]):
        sys.exit('You must provide a vaults directory.')


def decrypt_blob(mkp, blob):
    """Helper to decrypt blobs."""
    mks = mkp.getMasterKeys(blob.mkguid)
    if mks:
        for mk in mks:
            if mk.decrypted:
                blob.decrypt(mk.get_key())
                if blob.decrypted:
                    break
    else:
        print >> sys.stderr, 'MasterKey not found for blob.'

    if blob.decrypted:
        return blob.cleartext
    return None

    
def decrypt_vault_attribute(vault_attr, key_aes128, key_aes256):
    """Helper to decrypt VAULT attributes."""
    if not vault_attr.size:
        return '', False

    if vault_attr.vault_attr_encrypted.has_iv:
        cipher = AES.new(key_aes256, AES.MODE_CBC, vault_attr.vault_attr_encrypted.encrypted.iv)
        is_attribute_ex = True
    else:
        cipher = AES.new(key_aes128, AES.MODE_CBC)
        is_attribute_ex = False

    return cipher.decrypt(vault_attr.vault_attr_encrypted.encrypted.data), is_attribute_ex


def get_vault_schema(guid, base_dir, default_schema):
    '''Helper to get the Vault schema to apply on decoded data.'''
    vault_schema = default_schema
    schema_file_path = os.path.join(base_dir, guid + '.vsch')
    try:
        with open(schema_file_path, 'rb') as fschema:
            vsch = vaultschema.VAULT_VSCH.parse(fschema.read())
            vault_schema = vaultschema.vault_schemas.get(
                vsch.schema_name.data,
                vaultschema.VAULT_SCHEMA_GENERIC)
    except IOError:
        pass
    return vault_schema


if __name__ == '__main__':
    """Utility core."""
    usage = (
        'usage: %prog [options] Vault Directory\n\n'
        'It tries to decrypt Vault VCRD files.\n'
        'Can work with user MK *or* system MKs')

    parser = optparse.OptionParser(usage=usage)
    parser.add_option('--sid', metavar='SID', dest='sid')
    parser.add_option('--masterkey', metavar='DIRECTORY', dest='masterkeydir')
    parser.add_option('--credhist', metavar='FILE', dest='credhist')
    parser.add_option('--password', metavar='PASSWORD', dest='password')
    parser.add_option('--pwdhash', metavar='HASH', dest='pwdhash')
    parser.add_option('--system', metavar='HIVE', dest='system')
    parser.add_option('--security', metavar='HIVE', dest='security')

    (options, args) = parser.parse_args()

    check_parameters(options, args)

    mkp = masterkey.MasterKeyPool()
    mkp.loadDirectory(options.masterkeydir)

    if options.security and options.system:
        reg = registry.Regedit()
        secrets = reg.get_lsa_secrets(options.security, options.system)
        dpapi_system = secrets.get('DPAPI_SYSTEM')['CurrVal']
        mkp.addSystemCredential(dpapi_system)
        mkp.try_credential_hash(None, None)
    else:
        if options.credhist:
            mkp.addCredhistFile(options.sid, options.credhist)
        if options.password:
            mkp.try_credential(options.sid, options.password)
        elif options.pwdhash:
            mkp.try_credential_hash(options.sid, options.pwdhash.decode('hex'))

    vaults_dir = args[0]
    vpol_filename = os.path.join(os.path.sep, vaults_dir, 'Policy.vpol')

    with open(vpol_filename, 'rb') as fin:
        vpol = vaultstruct.VAULT_POL.parse(fin.read())

    vpol_blob = blob.DPAPIBlob(vaultstruct.DPAPI_BLOB_STRUCT.build(vpol.vpol_store.blob_store.raw))

    vpol_decrypted = decrypt_blob(mkp, vpol_blob)
    if not vpol_decrypted:
        sys.exit('Unable to decrypt blob.')

    vpol_keys = vaultstruct.VAULT_POL_KEYS.parse(vpol_decrypted)

    key_aes128 = vpol_keys.vpol_key1.bcrypt_blob.key
    key_aes256 = vpol_keys.vpol_key2.bcrypt_blob.key
    
    for file in os.listdir(vaults_dir):
        if file.lower().endswith('.vcrd'):
            filepath = os.path.join(vaults_dir, file)
            print '-'*79
            print 'Working on: %s\n' % file

            attributes_data = {}

            with open(filepath, 'rb') as fin:
                vcrd = vaultstruct.VAULT_VCRD.parse(fin.read())

                current_vault_schema = get_vault_schema(
                    vcrd.schema_guid,
                    vaults_dir,
                    vaultschema.VAULT_SCHEMA_GENERIC)
                
                for attribute in vcrd.attributes:
                    
                    decrypted, is_attribute_ex = decrypt_vault_attribute(
                        attribute.pointer, key_aes128, key_aes256)

                    if is_attribute_ex:
                        schema = current_vault_schema
                    else:
                        schema = vaultschema.VAULT_SCHEMA_SIMPLE

                    attributes_data[attribute.pointer.id] = {
                        'data': decrypted,
                        'schema': schema
                    }

                attributes_data[0xDEAD0000 + vcrd.extra_entry.id] = {
                    'data': str(vcrd.extra_entry.data),
                    'schema': vaultschema.VAULT_SCHEMA_SIMPLE
                }

            for k,v in sorted(attributes_data.iteritems()):
                dataout = v['schema'].parse(v['data'])
                print '{0:s}\n'.format(dataout)

