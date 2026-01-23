import os
import pickle
import http.cookiejar
import logging
import tempfile
import base64

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class CookieLoadError(Exception):
    """Custom exception for cookie loading failures."""
    pass

def load_cookies_safely(cookie_file='cookies.txt', domain_key=None, env_var='GOOGLE_COOKIES'):
    """
    Robust cookie loading strategy for Production and Dev environments.
    
    Priority:
    1. Environment Variable (base64 encoded Netscape content) - Best for Cloud/Docker.
    2. Local Netscape File ('cookies.txt') - Standard export format.
    3. Local Pickle File ('cookies.pkl') - Legacy Python format.
    4. Browser Extraction (browser_cookie3) - Local Dev fallback only.

    Args:
        cookie_file (str): Path to a cookie file (txt or pkl).
        domain_key (str, optional): Filter cookies for this domain.
        env_var (str): Name of environment variable containing base64 encoded cookies.

    Returns:
        http.cookiejar.CookieJar: The loaded cookies.
    """
    
    # 1. Try Environment Variable (Production / Cloud Run)
    if env_var and os.environ.get(env_var):
        logger.info(f"Found environment variable {env_var}. Attempting to load cookies...")
        try:
            # Decode base64 content to a temporary file
            content = base64.b64decode(os.environ[env_var]).decode('utf-8')
            with tempfile.NamedTemporaryFile(mode='w', delete=False) as tmp:
                tmp.write(content)
                tmp_path = tmp.name
            
            cj = http.cookiejar.MozillaCookieJar(tmp_path)
            cj.load(ignore_discard=True, ignore_expires=True)
            os.remove(tmp_path) # Cleanup
            
            logger.info("Successfully loaded cookies from environment variable.")
            return cj
        except Exception as e:
            logger.error(f"Failed to load cookies from environment variable: {e}")

    # 2. Try Local File (Production / Manual Export)
    if os.path.exists(cookie_file):
        logger.info(f"Found cookie file: {cookie_file}. Attempting to load...")
        try:
            # Check extension to decide loader
            if cookie_file.endswith('.pkl'):
                with open(cookie_file, 'rb') as f:
                    cj = pickle.load(f)
            else:
                # Assume Netscape/Mozilla format (standard cookies.txt)
                cj = http.cookiejar.MozillaCookieJar(cookie_file)
                cj.load(ignore_discard=True, ignore_expires=True)
                
            logger.info(f"Successfully loaded cookies from {cookie_file}.")
            return cj
        except Exception as e:
            logger.warning(f"Failed to load cookies from {cookie_file}: {e}. Proceeding to fallback.")
    else:
        logger.info(f"No cookie file found at {cookie_file}.")

    # 3. Try Browser Extraction (Last Resort - Local Dev Only)
    try:
        import browser_cookie3
        logger.info("Attempting to load cookies from Chrome via browser_cookie3...")
        cj = browser_cookie3.chrome(domain_name=domain_key)
        logger.info("Successfully extracted cookies from browser.")
        return cj
    except ImportError:
         msg = "browser_cookie3 not installed and no valid cookie sources (Env/File) found."
         logger.error(msg)
         raise CookieLoadError(msg)
    except Exception as e:
        logger.error(f"Failed to extract cookies from browser: {e}")
        # Identify common MacOS keychain error
        if "security" in str(e).lower() or "keychain" in str(e).lower() or "not authorized" in str(e).lower():
            hint = (
                "\n\n[HINT] MacOS Keychain access blocked.\n"
                "PRODUCTION FIX: Export cookies to 'cookies.txt' using a Chrome Extension (Netscape format).\n"
                "Then place 'cookies.txt' in this folder OR set GOOGLE_COOKIES env var.\n"
            )
            raise CookieLoadError(f"Browser extraction failed.{hint}") from e
        else:
             raise CookieLoadError(f"All cookie loading methods failed: {e}")

if __name__ == "__main__":
    try:
        # Default looks for cookies.txt now
        jar = load_cookies_safely('cookies.txt')
        print(f"Loaded {len(jar)} cookies.")
    except CookieLoadError as e:
        print(f"CRITICAL ERROR: {e}")
