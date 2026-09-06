import json
import os
import sys
import logging
import base64
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'settings'))
import config_parser as parser

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

TARGET_COUNTRIES = {
    'AT': 'Austria',
    'AU': 'Australia',
    'BE': 'Belgium',
    'CA': 'Canada',
    'CH': 'Switzerland',
    'CZ': 'Czechia',
    'DE': 'Germany',
    'DK': 'Denmark',
    'ES': 'Spain',
    'FR': 'France',
    'GB': 'United Kingdom',
    'ID': 'Indonesia',
    'IE': 'Ireland',
    'IN': 'India',
    'IT': 'Italy',
    'JP': 'Japan',
    'LT': 'Lithuania',
    'NL': 'Netherlands',
    'NO': 'Norway',
    'PL': 'Poland',
    'RO': 'Romania',
    'RS': 'Serbia',
    'SE': 'Sweden',
    'SG': 'Singapore',
    'US': 'United States'
}

def decode_flag_to_country_code(flag: str) -> str:
    try:
        chars = list(flag)
        if len(chars) != 2:
            return ''
        code = ''.join(chr(ord(c) - 0x1F1E6 + ord('A')) for c in chars)
        if len(code) == 2 and code.isalpha():
            return code.upper()
        return ''
    except Exception:
        return ''

def load_address_country_map(location_cache_path: str) -> dict:
    address_country = {}
    try:
        with open(location_cache_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        for address, value in data.items():
            if isinstance(value, (list, tuple)) and len(value) >= 1:
                code = decode_flag_to_country_code(value[0])
                if code:
                    address_country[address] = code
    except FileNotFoundError:
        logger.error(f"{location_cache_path} not found")
    except Exception as e:
        logger.error(f"Error loading {location_cache_path}: {e}")
    return address_country

def build_header(country_code: str) -> str:
    title = base64.b64encode(f"\U0001F47DAnonymous-{country_code}".encode('utf-8')).decode('utf-8')
    return f"""//profile-title: base64:{title}
//profile-update-interval: 1
//subscription-userinfo: upload=0; download=0; total=10737418240000000; expire=2546249531
//support-url: https://t.me/An0nymou3Bot
//profile-web-page-url: https://github.com/4n0nymou3

"""

def process(input_file: str, location_cache_file: str, output_root: str):
    try:
        with open(input_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except FileNotFoundError:
        logger.error(f"{input_file} not found!")
        return
    except Exception as e:
        logger.error(f"Error reading {input_file}: {e}")
        return

    configs = []
    for line in lines:
        line = line.strip()
        if not line.startswith('//') and line:
            configs.append(line)

    address_country = load_address_country_map(location_cache_file)

    grouped = {cc: [] for cc in TARGET_COUNTRIES}

    for config in configs:
        address = parser.extract_address(config)
        if not address:
            continue
        cc = address_country.get(address)
        if cc and cc in grouped:
            grouped[cc].append(config)

    for cc in TARGET_COUNTRIES:
        folder = os.path.join(output_root, cc)
        try:
            os.makedirs(folder, exist_ok=True)
        except Exception as e:
            logger.error(f"Failed to create folder {folder}: {e}")
            continue

        output_file = os.path.join(folder, 'proxy_configs.txt')
        matched = grouped[cc]

        if matched:
            try:
                with open(output_file, 'w', encoding='utf-8') as f:
                    f.write(build_header(cc))
                    for config in matched:
                        f.write(config + '\n\n')
                logger.info(f"{cc}: saved {len(matched)} configs to {output_file}")
            except Exception as e:
                logger.error(f"Failed to write {output_file}: {e}")
        else:
            if os.path.exists(output_file):
                logger.info(f"{cc}: no configs matched this run, keeping previous file untouched")
            else:
                try:
                    with open(output_file, 'w', encoding='utf-8') as f:
                        f.write(build_header(cc))
                    logger.info(f"{cc}: no configs matched, created empty file")
                except Exception as e:
                    logger.error(f"Failed to write {output_file}: {e}")

def main():
    if len(sys.argv) != 4:
        logger.error("Usage: python country_splitter.py <tested_configs_file> <location_cache_file> <output_root>")
        sys.exit(1)

    input_file = sys.argv[1]
    location_cache_file = sys.argv[2]
    output_root = sys.argv[3]
    process(input_file, location_cache_file, output_root)

if __name__ == '__main__':
    main()
