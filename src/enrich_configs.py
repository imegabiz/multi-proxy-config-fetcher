import json
import os
import sys
import logging
import base64
import socket
import requests
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Optional, Tuple, List, Set
from urllib.parse import urlparse, parse_qs
from collections import Counter
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'settings'))
from user_settings import LOCATION_APIS
import config_parser as parser

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class ConfigEnricher:
    DISABLE_AFTER_CONSECUTIVE_FAILURES = 5
    MAX_WORKERS = 20
    MAX_CONCURRENT_PER_DOMAIN = 4

    def __init__(self):
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'en-US,en;q=0.9'
        }
        self.resolved_locations: Dict[str, Tuple[str, str]] = {}
        self.previous_locations: Dict[str, Tuple[str, str]] = {}
        self.location_apis = self._initialize_apis()
        self.successful_patterns: Dict[str, str] = {}
        self.session = requests.Session()
        self.session.headers.update(self.headers)
        self.consecutive_failures: Dict[str, int] = {}
        self.disabled_domains: Set[str] = set()
        self.cache_lock = threading.Lock()
        self.domain_semaphores: Dict[str, threading.Semaphore] = {
            api['domain']: threading.Semaphore(self.MAX_CONCURRENT_PER_DOMAIN)
            for api in self.location_apis
        }


    def _clean_domain(self, api_input: str) -> str:
        api_input = api_input.strip()
        for prefix in ['https://', 'http://']:
            if api_input.startswith(prefix):
                api_input = api_input[len(prefix):]
        api_input = api_input.rstrip('/')
        if '/' in api_input:
            api_input = api_input.split('/')[0]
        return api_input.lower()

    def _generate_url_patterns(self, domain: str, ip: str) -> List[str]:
        patterns = []
        
        protocols = ['https', 'http']
        
        path_templates = [
            '?ip={ip}',
            '/?ip={ip}',
            '/{ip}',
            '/{ip}/json',
            '/{ip}/json/',
            '/json/{ip}',
            '/json?ip={ip}',
            '/api/{ip}',
            '/api/json/{ip}',
            '/api?ip={ip}',
            '/v1/{ip}',
            '/v1/json/{ip}',
            '/v2/{ip}',
            '/lookup/{ip}',
            '/geoip/{ip}',
            '/locate/{ip}',
            '/ip/{ip}',
            '/query/{ip}',
            '/{ip}.json',
            '/ip-country?ip={ip}',
            '?cmd=ip-country&ip={ip}'
        ]
        
        for protocol in protocols:
            for template in path_templates:
                path = template.format(ip=ip)
                patterns.append(f'{protocol}://{domain}{path}')
        
        return patterns

    def _extract_country_data(self, data: dict) -> Tuple[str, str]:
        if not isinstance(data, dict):
            return '', ''
        
        data_flat = {}
        for key, value in data.items():
            if value is not None:
                data_flat[key.lower()] = value
        
        status_indicators = data_flat.get('status', '').lower()
        if status_indicators in ['fail', 'error', 'failed']:
            return '', ''
        
        if data_flat.get('error') or data_flat.get('error_message'):
            return '', ''
        
        response_code = str(data_flat.get('response_code', ''))
        if response_code and response_code != '200':
            return '', ''
        
        country_code = ''
        country_name = ''
        
        code_fields = [
            'countrycode', 'country_code', 'country_code2', 
            'country_iso_code', 'iso_code', 'cc', 'code', 
            'country_iso', 'iso', 'countryisocode', 'cca2'
        ]
        
        for field in code_fields:
            if field in data_flat:
                value = str(data_flat[field]).strip().upper()
                if value and len(value) == 2 and value.isalpha():
                    country_code = value.lower()
                    break
        
        name_fields = [
            'country', 'country_name', 'countryname', 'name', 
            'country_long', 'countrylong', 'full_country_name'
        ]
        
        for field in name_fields:
            if field in data_flat:
                value = str(data_flat[field]).strip()
                if value and len(value) > 2 and not value.isdigit():
                    country_name = value
                    break
        
        return country_code, country_name

    def _initialize_apis(self) -> List[Dict[str, str]]:
        apis = []
        for api_input in LOCATION_APIS:
            try:
                domain = self._clean_domain(api_input)
                if domain:
                    apis.append({
                        'domain': domain,
                        'original': api_input
                    })
                    logger.info(f"Registered API: {domain}")
            except Exception as e:
                logger.warning(f"Failed to register '{api_input}': {e}")
        
        if not apis:
            logger.error("No location APIs configured!")
        
        return apis

    def _test_url(self, url: str, retries: int = 2) -> Optional[dict]:
        for attempt in range(retries):
            try:
                response = self.session.get(
                    url, 
                    timeout=5, 
                    allow_redirects=True
                )
                
                if response.status_code == 200:
                    content_type = response.headers.get('content-type', '').lower()
                    
                    if 'json' in content_type or 'application/json' in content_type:
                        try:
                            return response.json()
                        except json.JSONDecodeError:
                            pass
                    else:
                        try:
                            return response.json()
                        except:
                            pass
            except requests.exceptions.Timeout:
                logger.debug(f"Timeout for {url} (attempt {attempt + 1}/{retries})")
                if attempt < retries - 1:
                    time.sleep(0.5)
            except requests.exceptions.RequestException as e:
                logger.debug(f"Request error for {url}: {e}")
                break
            except Exception as e:
                logger.debug(f"Unexpected error for {url}: {e}")
                break
        
        return None

    def get_location_from_api(self, ip: str, api_config: dict) -> Tuple[str, str]:
        domain = api_config['domain']
        semaphore = self.domain_semaphores.get(domain)

        with self.cache_lock:
            if domain in self.disabled_domains:
                return '', ''
            cached_pattern = self.successful_patterns.get(domain)

        if cached_pattern:
            url = cached_pattern.format(ip=ip)
            if semaphore:
                with semaphore:
                    data = self._test_url(url)
            else:
                data = self._test_url(url)

            if data:
                country_code, country_name = self._extract_country_data(data)
                if country_code and country_name:
                    with self.cache_lock:
                        self.consecutive_failures[domain] = 0
                    return country_code, country_name

            with self.cache_lock:
                self.consecutive_failures[domain] = self.consecutive_failures.get(domain, 0) + 1
                if self.consecutive_failures[domain] >= self.DISABLE_AFTER_CONSECUTIVE_FAILURES:
                    logger.warning(
                        f"Disabling {domain} after {self.consecutive_failures[domain]} consecutive failed lookups"
                    )
                    self.disabled_domains.add(domain)
            logger.debug(f"Failed: {domain} - known-good pattern failed for this address")
            return '', ''

        url_patterns = self._generate_url_patterns(domain, ip)

        for url in url_patterns:
            if semaphore:
                with semaphore:
                    data = self._test_url(url)
            else:
                data = self._test_url(url)

            if data:
                country_code, country_name = self._extract_country_data(data)

                if country_code and country_name and len(country_code) == 2:
                    template = url.replace(ip, '{ip}')
                    with self.cache_lock:
                        self.successful_patterns[domain] = template
                        self.consecutive_failures[domain] = 0
                    logger.debug(f"Success: {domain} -> {template}")
                    return country_code, country_name

        with self.cache_lock:
            self.consecutive_failures[domain] = self.consecutive_failures.get(domain, 0) + 1
            if self.consecutive_failures[domain] >= self.DISABLE_AFTER_CONSECUTIVE_FAILURES:
                logger.warning(
                    f"Disabling {domain} after {self.consecutive_failures[domain]} consecutive failed lookups"
                )
                self.disabled_domains.add(domain)
        logger.debug(f"Failed: {domain} - no working pattern for this address")
        return '', ''

    def get_location(self, address: str) -> tuple:
        with self.cache_lock:
            if address in self.resolved_locations:
                return self.resolved_locations[address]

        try:
            ip = socket.gethostbyname(address)
        except socket.gaierror as e:
            logger.warning(f"Cannot resolve: {address} - {e}")
            previous = self.previous_locations.get(address)
            result = tuple(previous) if previous else ("🏳️", "Unknown")
            with self.cache_lock:
                self.resolved_locations[address] = result
            return result

        answers: List[Tuple[int, str, str]] = []
        for idx, api_config in enumerate(self.location_apis):
            if api_config['domain'] in self.disabled_domains:
                continue

            country_code, country = self.get_location_from_api(ip, api_config)

            if country_code and country and len(country_code) == 2:
                answers.append((idx, country_code, country))

        if answers:
            counts = Counter(code for _, code, _ in answers)
            top_count = max(counts.values())
            top_codes = {code for code, count in counts.items() if count == top_count}
            best = min((a for a in answers if a[1] in top_codes), key=lambda a: a[0])
            chosen_code, country_name = best[1], best[2]

            try:
                flag = ''.join(chr(0x1F1E6 + ord(c.upper()) - ord('A')) for c in chosen_code)
            except Exception:
                flag = "🏳️"

            result = (flag, country_name)
            with self.cache_lock:
                self.resolved_locations[address] = result
            logger.debug(
                f"{address} -> {flag} {country_name} (agreement {counts[chosen_code]}/{len(answers)})"
            )
            return result

        previous = self.previous_locations.get(address)
        if previous:
            logger.info(f"No geolocation API answered for {address} this run; keeping previous result")
            result = tuple(previous)
            with self.cache_lock:
                self.resolved_locations[address] = result
            return result

        logger.warning(f"Location unknown for {address}")
        result = ("🏳️", "Unknown")
        with self.cache_lock:
            self.resolved_locations[address] = result
        return result

    def extract_address(self, config: str) -> Optional[str]:
        try:
            config_lower = config.lower()
            data = None
            
            if config_lower.startswith('vmess://'):
                data = parser.decode_vmess(config)
                if data and 'add' in data:
                    return data['add']
            
            elif config_lower.startswith('vless://'):
                data = parser.parse_vless(config)
            
            elif config_lower.startswith('trojan://'):
                data = parser.parse_trojan(config)
            
            elif config_lower.startswith(('hysteria2://', 'hy2://')):
                data = parser.parse_hysteria2(config)
            
            elif config_lower.startswith('ss://'):
                data = parser.parse_shadowsocks(config)
            
            if data and 'address' in data:
                return data['address']
            
            return None
        except Exception as e:
            logger.debug(f"Failed to extract address from config: {e}")
            return None

    def process_configs(self, input_file: str, output_file: str):
        if os.path.exists(output_file):
            try:
                with open(output_file, 'r', encoding='utf-8') as f:
                    raw_previous = json.load(f)
                for key, value in raw_previous.items():
                    if isinstance(value, (list, tuple)) and len(value) >= 2 and value[1] and value[1] != 'Unknown':
                        self.previous_locations[key] = (value[0], value[1])
                logger.info(f"Loaded {len(self.previous_locations)} previous location entries as a fallback")
            except Exception as e:
                logger.warning(f"Could not load previous {output_file} for fallback: {e}")

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

        unique_addresses = set()
        for config in configs:
            address = self.extract_address(config)
            if address:
                unique_addresses.add(address)

        logger.info(f"Found {len(unique_addresses)} unique server addresses")

        total = len(unique_addresses)
        completed = 0
        progress_lock = threading.Lock()

        def _process(address: str):
            nonlocal completed
            try:
                self.get_location(address)
            except Exception as e:
                logger.warning(f"Error processing {address}: {e}")
            with progress_lock:
                completed += 1
                if completed % 50 == 0 or completed == total:
                    logger.info(f"Progress: {completed}/{total} addresses processed")

        with ThreadPoolExecutor(max_workers=self.MAX_WORKERS) as executor:
            futures = [executor.submit(_process, address) for address in unique_addresses]
            for future in as_completed(futures):
                future.result()

        cache_dict = {}
        for key, value in self.resolved_locations.items():
            cache_dict[key] = list(value)

        try:
            os.makedirs(os.path.dirname(output_file) or '.', exist_ok=True)
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(cache_dict, f, indent=2, ensure_ascii=False)
            logger.info(f"Successfully saved {len(cache_dict)} location entries to {output_file}")
        except IOError as e:
            logger.error(f"Failed to write output file: {e}")
        except Exception as e:
            logger.error(f"An unexpected error occurred: {e}")


def main():
    if len(sys.argv) < 3:
        print("Usage: python enrich_configs.py <input.txt> <output.json>")
        sys.exit(1)
    
    input_file = sys.argv[1]
    output_file = sys.argv[2]

    enricher = ConfigEnricher()
    enricher.process_configs(input_file, output_file)

if __name__ == '__main__':
    main()