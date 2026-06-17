# MQSMaster Trading Bot Codebase

This repository provides the foundation for a multi-portfolio trading bot and a backtesting framework.


## Table of Contents

- [Installation](#installation)
  - [Prerequisites](#prerequisites)
  - [Step 1: Install pip and uv](#step-1-install-pip-and-uv)
  - [Step 2: Download the Repository](#step-2-clone-the-repository)
  - [Step 3: Create and Activate the Virtual Environment](#step-3-create-and-activate-the-virtual-environment)
  - [Step 4: Install Project and Dependencies](#step-4-install-project-and-dependencies)
  - [Step 5: Set up Environment Variables](#step-5-set-up-environment-variables)
  - [Step 6: Test your Credentials](#step-6-test-your-credentials)
  - [Step 7: Download the FinBERT Model](#step-7-download-the-finbert-model-required-for-the-nlp-pipeline)


## Installation

This guide provides step-by-step instructions to clone, install, and run MQSMaster.


### Prerequisites

Before you begin, ensure you have the following software installed on your system:

1. `Git: For cloning the repository.`
1. `Python 3.12+: To run the application.(https://www.python.org/downloads/)`
1. `PgAdmin4 (Not mandatory, will work fine without).`

## Installation Steps

Follow these steps precisely to set up your environment.
### Install pip and uv
run the following command to install pip and uv:

1. `python3 -m pip install --upgrade pip`
1. `pip install uv`
1. `uv python list` # to check if uv is installed correctly
1. `uv python install python@3.12`
1. `uv python pin python@3.12`

#### Using Linux

If your computer is using Linux, the first two instructions may not work. Look up your distribution and the above packages for installation instructions.

### Step 2: Download the Repository
#### Downloading Using Git

Open your terminal, navigate to the directory where you want to store the project, and run the following:

1. `git clone https://github.com/MUNQuantSociety/MQSMaster.git`
1. `cd MQSMaster`


#### Download the .zip File

1. Download the code from the [git repository](https://github.com/MUNQuantSociety/MQSMaster), or [click here](https://github.com/MUNQuantSociety/MQSMaster/archive/refs/heads/main.zip) for the direct link to the download.
2. Extract the folder.


### Step 3: Create and Activate the Virtual Environment

We will create a virtual environment named MQS to keep the project's dependencies isolated. Run the following:

1. `uv venv MQS`

then run:

1. `source MQS/bin/activate` # (On Windows, use this command instead: MQS\Scripts\activate)

You will know the environment is active when you see (MQS) at the beginning of your terminal prompt. You must ALWAYS have your environment active whenever running any code in here.

#### Step 3: Set Up Environment Variables

The bot requires API keys and database credentials. We manage these using a .env file.
Ask the exec for the Data & Infra team for this file.

Copy the `.env.example` file or rename to `.env` in the root directory, then update it with your credentials:

1. `cp .env.example .env`(or just rename the file)
1. Open the `.env` file and fill in the required values (API keys, database credentials, etc.).
#### Step 4: Install Project and Dependencies

This project uses a pyproject.toml file for setup. The following commands will install the bot as a local package and then install all required third-party libraries.

Install the project in "editable" mode

1. `uv pip install -e .`
1. `uv pip install --no-cache-dir --only-binary :all: -r requirements.txt`


### Step 5: Set Up Environment Variables

The bot requires API keys and database credentials. We manage these using a .env file.
Ask the exec for the Data & Infra team for this file.

Copy the `.env.example` file and rename it to `.env` in the root directory and update it with your credentials.


#### Setting up a New Database

1. Create a PostgreSQL user for the program, let's say *mqs_master*, _without superuser privileges_. Ensure it cannot create more roles either, for security reasons.
2. Create a database for the program, let's say *mqs_db*.
3. Open database configuration as the `postgres` user, either using `psql` or PgAdmin 4.
4. Add a password to the new user.
    - In `psql`, run `ALTER USER mqs_master WITH PASSWORD '{new password}';`, where {new password} is a password of your choice.
4. Give *mqs_master* full privileges with the following `psql` command: `GRANT ALL PRIVILEGES ON DATABASE mqs_db TO mqs_master;`.
5. Create a schema for the user: `CREATE SCHEMA {username} AUTHORIZATION {username}`, where {username} is your user's username. Using the same name for both the schema and the user simplifies things.
6. In the `.env` file of the repository, add your database credentials.


### Step 6: Test your credentials

Run the following commands:

1. `python -m src.common.database.test`

If you see something along the following:

```
2025-09-20 16:54:37,793 INFO: Database connection pool created successfully.
✅ PostgreSQL connection successful! Current DB Time: 2025-09-20 15:24:37.975847-04:00
```

If it throws an error, see if you missed a step or [submit an issue](https://github.com/MUNQuantSociety/MQSMaster/issues) to our GitHub repository.


### Step 7: Download the FinBERT Model (required for the NLP pipeline)

The fine-tuned FinBERT model is **not** in the repo. If you plan to run any of the NLP / sentiment code (`NLP/daemon.py`, `NLP/process_sentiment_pipeline.py`, etc.), download the model folder and drop it into `NLP/`:

1. Download from: <https://drive.google.com/drive/folders/1v7NjSuyFq4CTIctrw1bSv13JzkkMg1l8?usp=sharing>
2. Place the folder so the final path is:

   ```
   NLP/finbert-finetuned-final/
   ```

3. Confirm it contains the model weights, tokenizer, and `config.json`.

See [NLP/README.md](../NLP/README.md) and [docs/NLP/README.md](../docs/NLP/README.md) for details.
