import flet as ft
import pytz
import os
import requests
import json
import pproxy
import asyncio
import geoip2.database
from functools import lru_cache
from timezonefinder import TimezoneFinder
from playwright.async_api import async_playwright, BrowserContext

# --- Constants ---
COUNTRY_DATABASE_PATH = "GeoLite2-Country.mmdb"
CITY_DATABASE_PATH = "GeoLite2-City.mmdb"

# Screen resolutions available for configuration
SCREENS = (
    "800×600", "960×540", "1024×768", "1152×864", "1280×720", "1280×768",
    "1280×800", "1280×1024", "1366×768", "1408×792", "1440×900",
    "1400×1050", "1440×1080", "1536×864", "1600×900", "1600×1024",
    "1600×1200", "1680×1050", "1920×1080", "1920×1200", "2048×1152",
    "2560×1080", "2560×1440", "3440×1440"
)
# Supported languages for browser locale
LANGUAGES = (
    "en-US", "en-GB", "fr-FR", "ru-RU", "es-ES", "pl-PL", "pt-PT",
    "nl-NL", "zh-CN"
)
# All common timezones available
TIMEZONES = pytz.common_timezones
# Fetch a random user agent from a reliable source
USER_AGENT = requests.get(
    "https://raw.githubusercontent.com/microlinkhq/top-user-agents/refs/heads/master/src/index.json"
).json()[0]

# --- Helper Functions ---

async def save_cookies(context: BrowserContext, profile: str) -> None:
    """
    Saves the current browser context cookies to a JSON file for a given profile.

    Args:
        context: The Playwright BrowserContext object.
        profile: The name of the profile to save cookies for.
    """
    cookies = await context.cookies()

    # Remove 'sameSite' attribute as it can cause issues with some Playwright versions
    for cookie in cookies:
        cookie.pop("sameSite", None)

    os.makedirs("cookies", exist_ok=True)
    with open(f"cookies/{profile}.json", "w", encoding="utf-8") as f:
        json.dump(obj=cookies, fp=f, indent=4)

def parse_netscape_cookies(netscape_cookie_str: str) -> list[dict]:
    """
    Parses cookies from Netscape cookie file format into a list of dictionaries.

    Args:
        netscape_cookie_str: A string containing cookies in Netscape format.

    Returns:
        A list of dictionaries, where each dictionary represents a cookie.
    """
    cookies = []
    lines = netscape_cookie_str.strip().split("\n")

    for line in lines:
        if not line.startswith("#") and line.strip():
            parts = line.split()
            if len(parts) == 7:
                cookie = {
                    "domain": parts[0],
                    "httpOnly": parts[1].upper() == "TRUE",
                    "path": parts[2],
                    "secure": parts[3].upper() == "TRUE",
                    "expires": float(parts[4]),
                    "name": parts[5],
                    "value": parts[6]
                }
                cookies.append(cookie)
    return cookies

@lru_cache(maxsize=256)
def get_proxy_info(ip: str) -> dict:
    """
    Retrieves geographical and timezone information for a given IP address
    using local GeoLite2 databases.

    Args:
        ip: The IP address to look up.

    Returns:
        A dictionary containing 'country_code', 'city', and 'timezone'.
        Returns 'UNK' for unknown values.
    """
    country_code = "UNK"
    city = "UNK"
    timezone = None

    try:
        with geoip2.database.Reader(COUNTRY_DATABASE_PATH) as reader:
            response = reader.country(ip)
            country_code = response.country.iso_code
    except (geoip2.errors.AddressNotFoundError, FileNotFoundError):
        pass # Country database not found or IP not found

    try:
        with geoip2.database.Reader(CITY_DATABASE_PATH) as reader:
            response = reader.city(ip)
            city = response.city.name if response.city.name else "UNK"
            # Use timezonefinder for more accurate timezone lookup based on coordinates
            tf = TimezoneFinder()
            timezone = tf.timezone_at(lng=response.location.longitude, lat=response.location.latitude)
            if timezone is None:
                timezone = "UNK"
    except (geoip2.errors.AddressNotFoundError, FileNotFoundError):
        pass # City database not found or IP not found

    return {"country_code": country_code, "city": city, "timezone": timezone}

async def run_proxy(protocol: str, ip: str, port: int, login: str, password: str) -> None:
    """
    Starts a local proxy server (e.g., SOCKS5) to tunnel traffic through a remote proxy.

    Args:
        protocol: The protocol of the remote proxy (e.g., 'http', 'socks5').
        ip: The IP address of the remote proxy.
        port: The port of the remote proxy.
        login: The username for proxy authentication.
        password: The password for proxy authentication.
    """
    # Ensure the pproxy library is installed: pip install pproxy
    try:
        server = pproxy.Server("socks5://127.0.0.1:1337")
        remote_url = f"{protocol}://{login}:{password}@{ip}:{port}"
        remote = pproxy.Connection(remote_url)
        args = dict(rserver=[remote], verbose=print)
        await server.start_server(args)
    except Exception as e:
        print(f"Error starting proxy server: {e}")

async def run_browser(
    user_agent: str,
    height: int,
    width: int,
    timezone: str,
    lang: str,
    proxy: str | bool,
    cookies_path: str | bool,
    webgl: bool,
    vendor: str,
    cpu: int,
    ram: int,
    is_touch: bool,
    profile: str
) -> None:
    """
    Launches and configures a Playwright browser instance with specified settings.

    Args:
        user_agent: The User-Agent string to use for the browser.
        height: The viewport height.
        width: The viewport width.
        timezone: The timezone ID (e.g., 'America/New_York').
        lang: The locale string (e.g., 'en-US').
        proxy: Proxy string (e.g., 'http://user:pass@host:port') or False.
        cookies_path: Path to a JSON file containing cookies or False.
        webgl: Whether to enable WebGL.
        vendor: The string to spoof for navigator.vendor.
        cpu: The number to spoof for navigator.hardwareConcurrency.
        ram: The number to spoof for navigator.deviceMemory.
        is_touch: Whether to simulate touch events.
        profile: The profile name for saving/loading cookies.
    """
    async with async_playwright() as p:
        browser_args = [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-web-security",
            "--ignore-certificate-errors",
            "--disable-infobars",
            "--disable-extensions",
            "--disable-blink-features=AutomationControlled",
        ]

        if not webgl:
            browser_args.append("--disable-webgl")

        proxy_settings = None
        proxy_task = None

        if proxy:
            try:
                protocol = proxy.split("://")[0]
                proxy_address = proxy.split("://")[1]

                if "@" in proxy_address:
                    auth, host_port = proxy_address.split("@")
                    username, password = auth.split(":")
                    ip, port_str = host_port.split(":")
                    port = int(port_str)
                else:
                    # Assuming format like http://host:port:user:pass if no @
                    # This part might need adjustment based on actual proxy format variations
                    parts = proxy_address.split(":")
                    if len(parts) >= 4 and protocol in ["http", "https"]: # http://host:port:user:pass
                        ip = parts[0]
                        port = int(parts[1])
                        username = parts[2]
                        password = parts[3]
                    elif len(parts) == 3 and protocol == "socks5": # socks5://host:port:user:pass is uncommon, usually socks5://user:pass@host:port
                        ip = parts[0]
                        port = int(parts[1])
                        username = parts[2]
                        password = "" # Assuming no password if format is unexpected
                    else:
                        raise ValueError("Unsupported proxy format")


                if protocol == "http" or protocol == "https":
                    proxy_settings = {
                        "server": f"{ip}:{port}",
                        "username": username,
                        "password": password
                    }
                elif protocol in ["socks4", "socks5"]:
                    # Start a local proxy tunnel if using SOCKS
                    proxy_task = asyncio.create_task(
                        run_proxy(protocol, ip, port, username, password)
                    )
                    proxy_settings = {"server": "socks5://127.0.0.1:1337"}
                else:
                    print(f"Unsupported proxy protocol: {protocol}")

            except (ValueError, IndexError) as e:
                print(f"Error parsing proxy string '{proxy}': {e}")
                proxy_settings = None # Disable proxy if parsing fails

        browser = await p.chromium.launch(
            headless=False,
            proxy=proxy_settings,
            args=browser_args
        )

        context = await browser.new_context(
            user_agent=user_agent,
            viewport={"width": width, "height": height},
            locale=lang,
            timezone_id=timezone,
            has_touch=is_touch
        )

        # Inject custom properties into the navigator object
        await context.add_init_script(f"""
            Object.defineProperty(navigator, 'vendor', {{
                get: function() {{ return '{vendor}'; }}
            }});
        """)

        await context.add_init_script(f"""
            Object.defineProperty(navigator, 'hardwareConcurrency', {{
                get: function() {{ return {cpu}; }}
            }});
        """)

        await context.add_init_script(f"""
            Object.defineProperty(navigator, 'deviceMemory', {{
                get: function() {{ return {ram}; }}
            }});
        """)

        # Load cookies if a valid path is provided and the file exists
        cookies_parsed = []
        if cookies_path and os.path.isfile(cookies_path):
            try:
                with open(cookies_path, "r", encoding="utf-8") as f:
                    cookies_content = f.read()
                    try:
                        cookies_parsed = json.loads(cookies_content)
                    except json.JSONDecodeError:
                        # Try parsing as Netscape format if JSON fails
                        cookies_parsed = parse_netscape_cookies(cookies_content)
            except Exception as e:
                print(f"Error loading cookies from {cookies_path}: {e}")

        # Load cookies from profile-specific file if it exists and no external path was given
        elif os.path.isfile(f"cookies/{profile}.json"):
            try:
                with open(f"cookies/{profile}.json", "r", encoding="utf-8") as f:
                    cookies_parsed = json.loads(f.read())
            except (json.JSONDecodeError, FileNotFoundError) as e:
                print(f"Error loading profile cookies for {profile}: {e}")

        # Add loaded cookies to the context
        for cookie in cookies_parsed:
            cookie["sameSite"] = "Strict" # Ensure sameSite is set
            await context.add_cookies([cookie])

        page = await context.new_page()

        # Spoof navigator.webdriver to undefined
        await page.evaluate("Object.defineProperty(navigator, '__proto__', { get: () => ({ webdriver: undefined }) });")
        await page.evaluate("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")

        await page.goto("about:blank")

        try:
            # Wait indefinitely for the page to close
            await page.wait_for_event("close", timeout=0)
        finally:
            # Clean up the proxy task if it was started
            if proxy_task:
                proxy_task.cancel()
            # Save cookies for the current profile
            await save_cookies(context, profile)
            await browser.close()

# --- Flet UI Functions ---

def main(page: ft.Page):
    """
    Main function to set up the Flet application UI.
    """
    page.title = "StillL Browser"
    page.adaptive = True

    def config_load(profile: str):
        """Loads and runs a browser profile."""
        try:
            with open(f"config/{profile}.json", "r", encoding="utf-8") as f:
                config = json.load(f)

            # Extract screen dimensions
            screen_dims = config.get("screen_resolution", "1920×1080").split("×")
            screen_width = int(screen_dims[0])
            screen_height = int(screen_dims[1])

            asyncio.run(run_browser(
                user_agent=config.get("user-agent", USER_AGENT),
                height=screen_height,
                width=screen_width,
                timezone=config.get("timezone", "Europe/Moscow"),
                lang=config.get("lang", "ru-RU"),
                proxy=config.get("proxy", False),
                cookies_path=config.get("cookies", False),
                webgl=config.get("webgl", False),
                vendor=config.get("vendor", "Google Inc."),
                cpu=config.get("cpu", 6),
                ram=config.get("ram", 6),
                is_touch=config.get("is_touch", False),
                profile=profile.rsplit(".", 1)[0] # Use profile name without extension
            ))
        except FileNotFoundError:
            page.snack_bar = ft.SnackBar(ft.Text("Конфиг не найден!"), open=True)
            page.update()
        except Exception as e:
            page.snack_bar = ft.SnackBar(ft.Text(f"Ошибка загрузки конфига: {e}"), open=True)
            page.update()

    def delete_profile(profile_filename: str):
        """Deletes a profile configuration file and updates the UI."""
        try:
            os.remove(f"config/{profile_filename}")
            page.controls = get_config_content()
            page.update()
            page.snack_bar = ft.SnackBar(ft.Text("Профиль удален"), open=True)
        except OSError as e:
            page.snack_bar = ft.SnackBar(ft.Text(f"Ошибка удаления профиля: {e}"), open=True)
        page.update()

    def get_config_content() -> list[ft.Column]:
        """Generates UI elements for displaying saved browser profiles."""
        configs_ui = []
        config_dir = "config"
        os.makedirs(config_dir, exist_ok=True)

        profile_files = [f for f in os.listdir(config_dir) if f.endswith(".json")]

        for cfg_filename in profile_files:
            profile_name = cfg_filename.rsplit(".", 1)[0]
            try:
                with open(os.path.join(config_dir, cfg_filename), "r", encoding="utf-8") as f:
                    config = json.load(f)

                configs_ui.append(
                    ft.Container(
                        bgcolor=ft.Colors.WHITE24,
                        padding=20,
                        border_radius=20,
                        content=ft.Row(
                            [
                                ft.Row([
                                    ft.Text(profile_name, size=20, weight=ft.FontWeight.W_600),
                                    ft.FilledButton(
                                        text=config.get("lang", "N/A"),
                                        icon="language",
                                        bgcolor=ft.Colors.WHITE24,
                                        color=ft.Colors.WHITE,
                                        icon_color=ft.Colors.WHITE,
                                        style=ft.ButtonStyle(padding=10),
                                        disabled=True # Make info buttons non-interactive
                                    ),
                                    ft.FilledButton(
                                        text=config.get("timezone", "N/A"),
                                        icon="schedule",
                                        bgcolor=ft.Colors.WHITE24,
                                        color=ft.Colors.WHITE,
                                        icon_color=ft.Colors.WHITE,
                                        style=ft.ButtonStyle(padding=10),
                                        disabled=True
                                    )
                                ], alignment=ft.MainAxisAlignment.START),
                                ft.Row([
                                    ft.IconButton(
                                        icon=ft.Icons.DELETE,
                                        icon_color=ft.Colors.WHITE70,
                                        tooltip="Удалить профиль",
                                        on_click=lambda _, fn=cfg_filename: delete_profile(fn)
                                    ),
                                    ft.FilledButton(
                                        text="Старт",
                                        icon="play_arrow",
                                        style=ft.ButtonStyle(padding=10),
                                        on_click=lambda _, fn=cfg_filename: config_load(fn)
                                    )
                                ], alignment=ft.MainAxisAlignment.END)
                            ],
                            alignment=ft.MainAxisAlignment.SPACE_BETWEEN
                        )
                    )
                )
            except (FileNotFoundError, json.JSONDecodeError) as e:
                print(f"Error processing config file {cfg_filename}: {e}")
                # Optionally display an error indicator for this file

        if configs_ui:
            return [
                ft.Column(
                    controls=[
                        ft.Text("Конфиги", size=20),
                        ft.Column(controls=configs_ui, spacing=20)
                    ],
                    spacing=20,
                    expand=True,
                    scroll=ft.ScrollMode.ALWAYS,
                    alignment=ft.MainAxisAlignment.START,
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER
                )
            ]
        else:
            return [
                ft.Row(
                    [ft.Text("Нет сохраненных конфигов. Нажмите '+' для создания.", size=16)],
                    alignment=ft.MainAxisAlignment.CENTER,
                    expand=True
                )
            ]

    def get_proxy_list() -> list[str]:
        """Reads proxy lines from proxies.txt."""
        proxies = []
        proxy_file = "proxies.txt"
        try:
            with open(proxy_file, "r", encoding="utf-8") as f:
                for line in f.read().split("\n"):
                    if line.strip():
                        proxies.append(line.strip())
            return proxies
        except FileNotFoundError:
            # Create an empty proxies.txt if it doesn't exist
            with open(proxy_file, "w", encoding="utf-8") as f:
                pass
            return []

    def get_proxies_content() -> list[ft.Column]:
        """Generates UI elements for displaying proxy information."""
        proxies_ui = []
        proxy_lines = get_proxy_list()

        for line in proxy_lines:
            ip = "N/A"
            try:
                if "://" in line:
                    host_part = line.split("://")[1]
                    if "@" in host_part:
                        ip = host_part.split("@")[1].split(":")[0]
                    else:
                        ip = host_part.split(":")[0]
                else: # Handle cases without protocol prefix
                    ip = line.split(":")[0]

                info = get_proxy_info(ip)

                proxies_ui.append(
                    ft.Container(
                        bgcolor=ft.Colors.WHITE24,
                        padding=20,
                        border_radius=20,
                        content=ft.Row([
                            ft.Text(line, size=16, weight=ft.FontWeight.W_500),
                            ft.FilledButton(
                                text=info["country_code"],
                                icon="flag",
                                bgcolor=ft.Colors.WHITE24,
                                color=ft.Colors.WHITE,
                                icon_color=ft.Colors.WHITE,
                                style=ft.ButtonStyle(padding=10),
                                disabled=True
                            ),
                            ft.FilledButton(
                                text=info["city"],
                                icon="location_city",
                                bgcolor=ft.Colors.WHITE24,
                                color=ft.Colors.WHITE,
                                icon_color=ft.Colors.WHITE,
                                style=ft.ButtonStyle(padding=10),
                                disabled=True
                            ),
                            ft.FilledButton(
                                text=info["timezone"] if info["timezone"] else "Unknown",
                                icon="schedule",
                                bgcolor=ft.Colors.WHITE24,
                                color=ft.Colors.WHITE,
                                icon_color=ft.Colors.WHITE,
                                style=ft.ButtonStyle(padding=10),
                                disabled=True
                            )
                        ], alignment=ft.MainAxisAlignment.START)
                    )
                )
            except Exception as e:
                print(f"Error processing proxy line '{line}': {e}")
                # Optionally display an error indicator for this proxy

        if proxies_ui:
            return [
                ft.Column(
                    controls=[
                        ft.Text("Прокси", size=20),
                        ft.Column(controls=proxies_ui, spacing=20)
                    ],
                    spacing=20,
                    expand=True,
                    scroll=ft.ScrollMode.ALWAYS,
                    alignment=ft.MainAxisAlignment.START,
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER
                )
            ]
        else:
            return [
                ft.Row(
                    [ft.Text("Нет добавленных прокси. Добавьте их в proxies.txt.", size=16)],
                    alignment=ft.MainAxisAlignment.CENTER,
                    expand=True
                )
            ]

    # --- Global variables for config page elements ---
    # These are defined here to be accessible within open_config_page and save_config
    profile_name_field: ft.TextField
    user_agent_field: ft.TextField
    screen_dropdown: ft.Dropdown
    timezone_dropdown: ft.Dropdown
    language_dropdown: ft.Dropdown
    proxy_dropdown: ft.Dropdown
    cookies_field: ft.TextField
    webgl_switch: ft.Switch
    vendor_field: ft.TextField
    cpu_threads_field: ft.TextField
    ram_field: ft.TextField
    is_touch_switch: ft.Switch

    def save_config(e):
        """Saves the current configuration from the form to a JSON file."""
        profile_name = profile_name_field.value.strip()
        if not profile_name:
            page.snack_bar = ft.SnackBar(ft.Text("Имя профиля не может быть пустым!"), open=True)
            page.update()
            return

        user_agent_value = user_agent_field.value if user_agent_field.value else USER_AGENT
        screen_value = screen_dropdown.value if screen_dropdown.value else "1920×1080"
        timezone_value = timezone_dropdown.value if timezone_dropdown.value else "Europe/Moscow"
        language_value = language_dropdown.value if language_dropdown.value else "ru-RU"
        proxy_value = proxy_dropdown.value if proxy_dropdown.value else False
        cookies_value = cookies_field.value.strip() if cookies_field.value else False
        webgl_value = webgl_switch.value
        vendor_value = vendor_field.value if vendor_field.value else "Google Inc."
        
        try:
            cpu_threads_value = int(cpu_threads_field.value) if cpu_threads_field.value else 6
            ram_value = int(ram_field.value) if ram_field.value else 6
        except ValueError:
            page.snack_bar = ft.SnackBar(ft.Text("CPU и RAM должны быть числами!"), open=True)
            page.update()
            return
            
        is_touch_value = is_touch_switch.value

        config_data = {
            "user-agent": user_agent_value,
            "screen_resolution": screen_value, # Store as resolution string
            "timezone": timezone_value,
            "lang": language_value,
            "proxy": proxy_value,
            "cookies": cookies_value,
            "webgl": webgl_value,
            "vendor": vendor_value,
            "cpu": cpu_threads_value,
            "ram": ram_value,
            "is_touch": is_touch_value
        }

        config_dir = "config"
        os.makedirs(config_dir, exist_ok=True)
        profile_filename = f"{profile_name}.json"

        try:
            with open(os.path.join(config_dir, profile_filename), "w", encoding="utf-8") as f:
                json.dump(obj=config_data, fp=f, indent=4)

            page.controls = get_config_content()
            page.update()
            page.snack_bar = ft.SnackBar(ft.Text("Конфиг сохранен!"), open=True)
        except IOError as e:
            page.snack_bar = ft.SnackBar(ft.Text(f"Ошибка сохранения конфига: {e}"), open=True)
        page.update()

    def open_config_page(e):
        """Opens the configuration page for creating a new profile."""
        nonlocal profile_name_field, user_agent_field, screen_dropdown, timezone_dropdown, language_dropdown, proxy_dropdown, cookies_field, webgl_switch, vendor_field, cpu_threads_field, ram_field, is_touch_switch

        # Determine the next available profile number
        n = 1
        config_dir = "config"
        os.makedirs(config_dir, exist_ok=True)
        while os.path.exists(os.path.join(config_dir, f"Profile {n}.json")):
            n += 1

        # Initialize form fields
        profile_name_field = ft.TextField(
            label="Имя профиля",
            value=f"Profile {n}",
            border_color=ft.Colors.WHITE,
            border_radius=20,
            content_padding=10,
            width=250
        )
        user_agent_field = ft.TextField(
            hint_text="User Agent (оставьте пустым для случайного)",
            value=USER_AGENT,
            expand=True,
            border_color=ft.Colors.WHITE,
            border_radius=20,
            content_padding=10
        )
        screen_dropdown = ft.Dropdown(
            label="Разрешение экрана",
            value="1920×1080",
            width=250,
            border_color=ft.Colors.WHITE,
            border_radius=20,
            options=[ft.dropdown.Option(screen) for screen in SCREENS]
        )
        timezone_dropdown = ft.Dropdown(
            label="Часовой пояс",
            value="Europe/Moscow", # Default value
            width=250,
            border_color=ft.Colors.WHITE,
            border_radius=20,
            options=[ft.dropdown.Option(timezone) for timezone in TIMEZONES],
            height=50 # Adjust height for better appearance
        )
        language_dropdown = ft.Dropdown(
            label="Язык",
            value="ru-RU", # Default value
            width=200,
            border_color=ft.Colors.WHITE,
            border_radius=20,
            options=[ft.dropdown.Option(lang) for lang in LANGUAGES]
        )
        proxy_dropdown = ft.Dropdown(
            label="Прокси",
            hint_text="Выберите прокси из списка",
            expand=True,
            border_color=ft.Colors.WHITE,
            border_radius=20,
            options=[ft.dropdown.Option(proxy) for proxy in get_proxy_list()]
        )
        cookies_field = ft.TextField(
            hint_text="Путь к файлу cookies.json (или Netscape)",
            expand=True,
            border_color=ft.Colors.WHITE,
            border_radius=20,
            content_padding=10
        )
        webgl_switch = ft.Switch(
            adaptive=True,
            label="Включить WebGL",
            value=False,
        )
        vendor_field = ft.TextField(
            label="Производитель (navigator.vendor)",
            value="Google Inc.",
            expand=True,
            border_color=ft.Colors.WHITE,
            border_radius=20,
            content_padding=10
        )
        cpu_threads_field = ft.TextField(
            label="CPU Threads (hardwareConcurrency)",
            value="6",
            keyboard_type=ft.KeyboardType.NUMBER,
            border_color=ft.Colors.WHITE,
            border_radius=20,
            content_padding=10,
            width=200
        )
        ram_field = ft.TextField(
            label="RAM (deviceMemory)",
            value="6",
            keyboard_type=ft.KeyboardType.NUMBER,
            border_color=ft.Colors.WHITE,
            border_radius=20,
            content_padding=10,
            width=200
        )
        is_touch_switch = ft.Switch(
            adaptive=True,
            label="Имитировать касания (Touch)",
            value=False,
        )

        # Layout the configuration form
        page.controls = [
            ft.Column(
                controls=[
                    ft.Text("Новый конфиг", size=24, weight=ft.FontWeight.BOLD),
                    ft.Container(height=20), # Spacer
                    ft.Row(
                        [
                            profile_name_field,
                            ft.FilledButton(
                                text="Сохранить",
                                icon="check",
                                style=ft.ButtonStyle(padding=15),
                                on_click=save_config
                            )
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER
                    ),
                    ft.Container(height=10),
                    ft.Row(
                        [
                            user_agent_field,
                            screen_dropdown
                        ],
                        alignment=ft.MainAxisAlignment.CENTER
                    ),
                    ft.Container(height=10),
                    ft.Row(
                        [
                            timezone_dropdown,
                            language_dropdown,
                            proxy_dropdown
                        ],
                        alignment=ft.MainAxisAlignment.CENTER,
                        spacing=10
                    ),
                    ft.Container(height=10),
                    ft.Row(
                        [
                            cookies_field,
                            webgl_switch
                        ],
                        alignment=ft.MainAxisAlignment.CENTER
                    ),
                    ft.Container(height=10),
                    ft.Row(
                        [
                            vendor_field,
                            cpu_threads_field,
                            ram_field,
                            is_touch_switch
                        ],
                        alignment=ft.MainAxisAlignment.CENTER,
                        spacing=10
                    )
                ],
                spacing=15,
                alignment=ft.MainAxisAlignment.START,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                expand=True,
                scroll=ft.ScrollMode.AUTO
            )
        ]
        page.update()

    def update_content(e):
        """Updates the main content area based on the selected navigation item."""
        if e.control.selected_index == 0:
            page.appbar = ft.AppBar(
                title=ft.Text("StillL Browser"),
                actions=[
                    ft.IconButton(
                        ft.CupertinoIcons.ADD,
                        tooltip="Добавить новый профиль",
                        style=ft.ButtonStyle(padding=10),
                        on_click=open_config_page
                    )
                ],
                bgcolor=ft.Colors.with_opacity(0.04, ft.CupertinoColors.SYSTEM_BACKGROUND),
            )
            page.controls = get_config_content()
        elif e.control.selected_index == 1:
            page.appbar = ft.AppBar(
                title=ft.Text("StillL Browser"),
                actions=[], # No add button on proxy page
                bgcolor=ft.Colors.with_opacity(0.04, ft.CupertinoColors.SYSTEM_BACKGROUND),
            )
            page.controls = get_proxies_content()

        page.update()

    # --- Initial Setup ---
    page.appbar = ft.AppBar(
        title=ft.Text("StillL Browser"),
        actions=[
            ft.IconButton(
                ft.CupertinoIcons.ADD,
                tooltip="Добавить новый профиль",
                style=ft.ButtonStyle(padding=10),
                on_click=open_config_page
            )
        ],
        bgcolor=ft.Colors.with_opacity(0.04, ft.CupertinoColors.SYSTEM_BACKGROUND),
    )

    page.navigation_bar = ft.NavigationBar(
        on_change=update_content,
        destinations=[
            ft.NavigationBarDestination(icon=ft.Icons.TUNE, label="Конфиги"),
            ft.NavigationBarDestination(icon=ft.Icons.VPN_KEY, label="Прокси")
        ],
        border=ft.Border(
            top=ft.BorderSide(color=ft.CupertinoColors.SYSTEM_GREY2, width=0)
        ),
    )

    # Add initial content (config list)
    page.add(get_config_content()[0])


if __name__ == "__main__":
    # Ensure necessary directories exist
    os.makedirs("config", exist_ok=True)
    os.makedirs("cookies", exist_ok=True)

    # Download GeoIP databases if they don't exist
    if not os.path.isfile(COUNTRY_DATABASE_PATH):
        print("Downloading GeoLite2-Country.mmdb...")
        try:
            response = requests.get("https://git.io/GeoLite2-Country.mmdb", stream=True)
            response.raise_for_status() # Raise an exception for bad status codes
            with open(COUNTRY_DATABASE_PATH, "wb") as file:
                for chunk in response.iter_content(chunk_size=8192):
                    file.write(chunk)
            print("Download complete.")
        except requests.exceptions.RequestException as e:
            print(f"Error downloading GeoLite2-Country.mmdb: {e}")
            print("Please download it manually from MaxMind and place it in the script's directory.")

    if not os.path.isfile(CITY_DATABASE_PATH):
        print("Downloading GeoLite2-City.mmdb...")
        try:
            response = requests.get("https://git.io/GeoLite2-City.mmdb", stream=True)
            response.raise_for_status()
            with open(CITY_DATABASE_PATH, "wb") as file:
                for chunk in response.iter_content(chunk_size=8192):
                    file.write(chunk)
            print("Download complete.")
        except requests.exceptions.RequestException as e:
            print(f"Error downloading GeoLite2-City.mmdb: {e}")
            print("Please download it manually from MaxMind and place it in the script's directory.")

    # Start the Flet application
    ft.app(target=main)
