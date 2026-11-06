@echo off
setlocal

rem Edit these deployment values before use.
set "RESOURCE_GROUP_NAME=overseer-rg"
set "REGION=eastus"
set "STATIC_WEB_APP_REGION=eastus2"
set "CONTAINER_APP_NAME=overseer-api"
set "STATIC_WEB_APP_NAME=overseer-web"
set "ACR_NAME=overseerrgacr"
set "FOUNDRY_PROJECT_SCOPE=/subscriptions/d6e4e6dc-d576-4acd-955e-865ce88d5452/resourceGroups/overseer-rg/providers/Microsoft.CognitiveServices/accounts/Overseer1-resource/projects/Overseer1"
set "FOUNDRY_USER_ROLE_DEFINITION_ID=53ca6127-db72-4b80-b1b0-d745d6d5456d"

set "DEPLOY_MODE=%~1"
if not defined DEPLOY_MODE set "DEPLOY_MODE=all"
if /i not "%DEPLOY_MODE%"=="all" if /i not "%DEPLOY_MODE%"=="backend" if /i not "%DEPLOY_MODE%"=="frontend" goto usage

for %%I in ("%~dp0..") do set "REPO_ROOT=%%~fI"
set "ENV_FILE=%REPO_ROOT%\.env"
if not defined FOUNDRY_PROJECT_ENDPOINT set "FOUNDRY_PROJECT_ENDPOINT="
if not defined FOUNDRY_MODEL set "FOUNDRY_MODEL="
if not defined APPLICATIONINSIGHTS_CONNECTION_STRING set "APPLICATIONINSIGHTS_CONNECTION_STRING="
if not defined LLM_MAX_CONCURRENCY set "LLM_MAX_CONCURRENCY="
if not defined ANALYSIS_TICK_SECONDS set "ANALYSIS_TICK_SECONDS="
if not defined STREAM_DELAY_SECONDS set "STREAM_DELAY_SECONDS="

rem Existing process environment values take precedence over .env.
if exist "%ENV_FILE%" (
    for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%ENV_FILE%") do (
        if /i "%%A"=="FOUNDRY_PROJECT_ENDPOINT" if not defined FOUNDRY_PROJECT_ENDPOINT set "FOUNDRY_PROJECT_ENDPOINT=%%B"
        if /i "%%A"=="FOUNDRY_MODEL" if not defined FOUNDRY_MODEL set "FOUNDRY_MODEL=%%B"
        if /i "%%A"=="APPLICATIONINSIGHTS_CONNECTION_STRING" if not defined APPLICATIONINSIGHTS_CONNECTION_STRING set "APPLICATIONINSIGHTS_CONNECTION_STRING=%%B"
        if /i "%%A"=="LLM_MAX_CONCURRENCY" if not defined LLM_MAX_CONCURRENCY set "LLM_MAX_CONCURRENCY=%%B"
        if /i "%%A"=="ANALYSIS_TICK_SECONDS" if not defined ANALYSIS_TICK_SECONDS set "ANALYSIS_TICK_SECONDS=%%B"
        if /i "%%A"=="STREAM_DELAY_SECONDS" if not defined STREAM_DELAY_SECONDS set "STREAM_DELAY_SECONDS=%%B"
    )
)

if not defined LLM_MAX_CONCURRENCY set "LLM_MAX_CONCURRENCY=2"
if not defined ANALYSIS_TICK_SECONDS set "ANALYSIS_TICK_SECONDS=3"
if not defined STREAM_DELAY_SECONDS set "STREAM_DELAY_SECONDS=8"
set "STAGING_PATH="
set "DEPLOYMENT_TOKEN="

where az >nul 2>nul
if errorlevel 1 (
    echo ERROR: Azure CLI is required. Install it and run az login.
    exit /b 1
)
az account show --only-show-errors --output none
if errorlevel 1 exit /b 1

if /i "%DEPLOY_MODE%"=="frontend" goto frontend_only
call :deploy_backend
if errorlevel 1 goto failed
if /i "%DEPLOY_MODE%"=="backend" goto success
call :deploy_frontend
if errorlevel 1 goto failed
goto success

:frontend_only
call :deploy_frontend
if errorlevel 1 goto failed
goto success

:deploy_backend
if not defined FOUNDRY_PROJECT_ENDPOINT (
    echo ERROR: Set FOUNDRY_PROJECT_ENDPOINT in the process environment or .env.
    exit /b 1
)
if not defined FOUNDRY_MODEL (
    echo ERROR: Set FOUNDRY_MODEL in the process environment or .env.
    exit /b 1
)
if not "%FOUNDRY_PROJECT_ENDPOINT:~0,8%"=="https://" (
    echo ERROR: FOUNDRY_PROJECT_ENDPOINT must be an HTTPS Foundry project endpoint.
    exit /b 1
)
if "%FOUNDRY_PROJECT_ENDPOINT:~8,1%"=="<" (
    echo ERROR: FOUNDRY_PROJECT_ENDPOINT must not be a placeholder.
    exit /b 1
)
if "%FOUNDRY_MODEL:~0,1%"=="<" (
    echo ERROR: FOUNDRY_MODEL must be the actual Foundry deployment name.
    exit /b 1
)
if not exist "%REPO_ROOT%\Dockerfile" (
    echo ERROR: Dockerfile was not found in the repository root.
    exit /b 1
)
where docker >nul 2>nul
if errorlevel 1 (
    echo ERROR: Docker CLI is required to build and push the backend image.
    exit /b 1
)

echo Adding or upgrading the Azure Container Apps CLI extension...
az extension add --name containerapp --upgrade --only-show-errors --output none
if errorlevel 1 exit /b 1

echo Creating the resource group if needed...
az group create --name "%RESOURCE_GROUP_NAME%" --location "%REGION%" --only-show-errors --output none
if errorlevel 1 exit /b 1

echo Creating the Azure Container Registry if needed...
az acr create --name "%ACR_NAME%" --resource-group "%RESOURCE_GROUP_NAME%" --sku Basic --admin-enabled true --only-show-errors --output none
if errorlevel 1 exit /b 1

echo Logging in to ACR...
az acr login --name "%ACR_NAME%"
if errorlevel 1 exit /b 1

set "IMAGE_TAG=%ACR_NAME%.azurecr.io/%CONTAINER_APP_NAME%:latest"
echo Building the backend image...
pushd "%REPO_ROOT%"
docker build -t "%IMAGE_TAG%" .
if errorlevel 1 (
    popd
    exit /b 1
)
echo Pushing the backend image...
docker push "%IMAGE_TAG%"
if errorlevel 1 (
    popd
    exit /b 1
)
popd

echo Deploying the backend Container App...
az containerapp up --name "%CONTAINER_APP_NAME%" --resource-group "%RESOURCE_GROUP_NAME%" --location "%REGION%" --image "%IMAGE_TAG%" --registry-server "%ACR_NAME%.azurecr.io" --ingress external --target-port 8000 --env-vars "FOUNDRY_PROJECT_ENDPOINT=%FOUNDRY_PROJECT_ENDPOINT%" "FOUNDRY_MODEL=%FOUNDRY_MODEL%" "APPLICATIONINSIGHTS_CONNECTION_STRING=%APPLICATIONINSIGHTS_CONNECTION_STRING%" "LLM_MAX_CONCURRENCY=%LLM_MAX_CONCURRENCY%" "ANALYSIS_TICK_SECONDS=%ANALYSIS_TICK_SECONDS%" "STREAM_DELAY_SECONDS=%STREAM_DELAY_SECONDS%"
if errorlevel 1 exit /b 1

echo Pinning the Container App to one replica...
az containerapp update --name "%CONTAINER_APP_NAME%" --resource-group "%RESOURCE_GROUP_NAME%" --min-replicas 1 --max-replicas 1 --only-show-errors --output none
if errorlevel 1 exit /b 1

echo Assigning a system-managed identity...
az containerapp identity assign --name "%CONTAINER_APP_NAME%" --resource-group "%RESOURCE_GROUP_NAME%" --system-assigned --only-show-errors --output none
if errorlevel 1 exit /b 1
set "PRINCIPAL_ID="
for /f "delims=" %%A in ('az containerapp show --name "%CONTAINER_APP_NAME%" --resource-group "%RESOURCE_GROUP_NAME%" --query identity.principalId --output tsv --only-show-errors') do set "PRINCIPAL_ID=%%A"
if /i "%PRINCIPAL_ID%"=="None" set "PRINCIPAL_ID="
if not defined PRINCIPAL_ID (
    echo ERROR: Azure did not return the Container App identity principal ID.
    exit /b 1
)

set "ROLE_ASSIGNMENT="
for /f "delims=" %%A in ('az role assignment list --assignee-object-id "%PRINCIPAL_ID%" --scope "%FOUNDRY_PROJECT_SCOPE%" --role "%FOUNDRY_USER_ROLE_DEFINITION_ID%" --query "[0].id" --output tsv --only-show-errors') do set "ROLE_ASSIGNMENT=%%A"
if /i "%ROLE_ASSIGNMENT%"=="None" set "ROLE_ASSIGNMENT="
if not defined ROLE_ASSIGNMENT (
    echo Granting the Foundry User role on the configured project scope...
    az role assignment create --assignee-object-id "%PRINCIPAL_ID%" --assignee-principal-type ServicePrincipal --role "%FOUNDRY_USER_ROLE_DEFINITION_ID%" --scope "%FOUNDRY_PROJECT_SCOPE%" --only-show-errors --output none
    if errorlevel 1 exit /b 1
)
set "API_HOST="
for /f "delims=" %%A in ('az containerapp show --name "%CONTAINER_APP_NAME%" --resource-group "%RESOURCE_GROUP_NAME%" --query properties.configuration.ingress.fqdn --output tsv --only-show-errors') do set "API_HOST=%%A"
if /i "%API_HOST%"=="None" set "API_HOST="
if not defined API_HOST (
    echo ERROR: Azure did not return the Container App ingress hostname.
    exit /b 1
)
set "API_ORIGIN=https://%API_HOST%"
exit /b 0

:deploy_frontend
where swa >nul 2>nul
if errorlevel 1 (
    echo ERROR: Static Web Apps CLI is required. Install with npm install -g @azure/static-web-apps-cli.
    exit /b 1
)
set "API_HOST="
for /f "delims=" %%A in ('az containerapp show --name "%CONTAINER_APP_NAME%" --resource-group "%RESOURCE_GROUP_NAME%" --query properties.configuration.ingress.fqdn --output tsv --only-show-errors') do set "API_HOST=%%A"
if /i "%API_HOST%"=="None" set "API_HOST="
if not defined API_HOST (
    echo ERROR: Azure did not return the Container App ingress hostname.
    exit /b 1
)
set "API_ORIGIN=https://%API_HOST%"

echo Creating the Free Azure Static Web App if needed...
az staticwebapp create --name "%STATIC_WEB_APP_NAME%" --resource-group "%RESOURCE_GROUP_NAME%" --location "%STATIC_WEB_APP_REGION%" --sku Free --only-show-errors --output none
if errorlevel 1 exit /b 1
set "STATIC_HOST="
for /f "delims=" %%A in ('az staticwebapp show --name "%STATIC_WEB_APP_NAME%" --resource-group "%RESOURCE_GROUP_NAME%" --query defaultHostname --output tsv --only-show-errors') do set "STATIC_HOST=%%A"
if /i "%STATIC_HOST%"=="None" set "STATIC_HOST="
if not defined STATIC_HOST (
    echo ERROR: Azure did not return the Static Web App hostname.
    exit /b 1
)
set "STATIC_ORIGIN=https://%STATIC_HOST%"

echo Restricting API CORS to the Static Web App origin...
az containerapp update --name "%CONTAINER_APP_NAME%" --resource-group "%RESOURCE_GROUP_NAME%" --set-env-vars "CORS_ALLOWED_ORIGINS=%STATIC_ORIGIN%" --only-show-errors --output none
if errorlevel 1 exit /b 1
az containerapp ingress cors enable --name "%CONTAINER_APP_NAME%" --resource-group "%RESOURCE_GROUP_NAME%" --allowed-origins "%STATIC_ORIGIN%" --allowed-methods GET POST OPTIONS --allowed-headers Content-Type Last-Event-ID --only-show-errors --output none
if errorlevel 1 exit /b 1

set "DEPLOYMENT_TOKEN="
for /f "delims=" %%A in ('az staticwebapp secrets list --name "%STATIC_WEB_APP_NAME%" --resource-group "%RESOURCE_GROUP_NAME%" --query properties.apiKey --output tsv --only-show-errors') do set "DEPLOYMENT_TOKEN=%%A"
if not defined DEPLOYMENT_TOKEN (
    echo ERROR: Azure did not return a Static Web Apps deployment token.
    exit /b 1
)
if /i "%DEPLOYMENT_TOKEN%"=="None" (
    echo ERROR: Azure did not return a Static Web Apps deployment token.
    exit /b 1
)

set "STAGING_PATH=%TEMP%\overseer-web-%RANDOM%-%RANDOM%"
mkdir "%STAGING_PATH%"
if errorlevel 1 exit /b 1
xcopy "%REPO_ROOT%\web\*" "%STAGING_PATH%\" /E /I /Y >nul
if errorlevel 1 exit /b 1
>"%STAGING_PATH%\config.js" echo window.OVERSEER_API_BASE_URL = "%API_ORIGIN%";
if errorlevel 1 exit /b 1

set "SWA_CLI_DEPLOYMENT_TOKEN=%DEPLOYMENT_TOKEN%"
echo Deploying the frontend...
swa deploy "%STAGING_PATH%" --env production
if errorlevel 1 exit /b 1
set "SWA_CLI_DEPLOYMENT_TOKEN="
set "DEPLOYMENT_TOKEN="
exit /b 0

:success
if defined STAGING_PATH if exist "%STAGING_PATH%" rmdir /s /q "%STAGING_PATH%"
set "SWA_CLI_DEPLOYMENT_TOKEN="
echo Deployment completed.
if /i not "%DEPLOY_MODE%"=="frontend" (
    echo Backend redeployed with minimum and maximum replicas set to 1.
)
if defined API_ORIGIN echo API: %API_ORIGIN%
if defined STATIC_ORIGIN echo Dashboard: %STATIC_ORIGIN%
exit /b 0

:failed
if defined STAGING_PATH if exist "%STAGING_PATH%" rmdir /s /q "%STAGING_PATH%"
set "SWA_CLI_DEPLOYMENT_TOKEN="
set "DEPLOYMENT_TOKEN="
echo Deployment failed. Review the preceding command output.
exit /b 1

:usage
echo Usage: infra\deploy.bat [all^|backend^|frontend]
exit /b 2
