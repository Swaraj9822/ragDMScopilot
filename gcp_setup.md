
1. Install the Google Cloud SDK (gcloud CLI)

Download from https://cloud.google.com/sdk/docs/install and run the installer for your OS.

2. Authenticate

gcloud auth login
This opens a browser for OAuth. Once done, your user credentials are stored locally.

3. Set Application Default Credentials (ADC)

gcloud auth application-default login
This is what the Python SDK (google-cloud-aiplatform) uses to authenticate API calls. It writes a credentials JSON file to a well-known location (~/.config/gcloud/application_default_credentials.json on Linux/Mac, %APPDATA%\gcloud\application_default_credentials.json on Windows).

4. Set your project

gcloud config set project project-619b14fd-4c6b-4f0a-b60
5. Enable the Vertex AI API (one-time per project)

gcloud services enable aiplatform.googleapis.com
6. Configure your .env

LLM_PROVIDER=gemini
GEMINI_MODEL_ID=gemini-3.1-pro-preview
GCP_PROJECT_ID=project-619b14fd-4c6b-4f0a-b60
GCP_LOCATION=global
That's it. You don't need GOOGLE_APPLICATION_CREDENTIALS in .env if you use ADC (step 3) — the SDK picks it up automatically.

Alternative: Service Account (for CI/Docker/production)

If you're running in a headless environment:

Create a service account in GCP Console with the "Vertex AI User" role
Download the JSON key file
Set GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json in .env
The ADC approach (steps 2-3) is best for local dev. Service account keys are for non-interactive environments.