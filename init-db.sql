-- Initialize databases and users for platform services
-- This runs automatically on first postgres start

-- Create IEOMD database and user
CREATE USER ieomd WITH PASSWORD 'CHANGE_ME_IEOMD';
CREATE DATABASE ieomd_db OWNER ieomd;
GRANT ALL PRIVILEGES ON DATABASE ieomd_db TO ieomd;

-- Create Umami database and user
CREATE USER umami WITH PASSWORD 'CHANGE_ME_UMAMI';
CREATE DATABASE umami_db OWNER umami;
GRANT ALL PRIVILEGES ON DATABASE umami_db TO umami;

-- Create Synapse (Matrix) database and user
CREATE USER synapse WITH PASSWORD 'CHANGE_ME_SYNAPSE';
CREATE DATABASE synapse_db
  OWNER synapse
  ENCODING 'UTF8'
  LC_COLLATE='C'
  LC_CTYPE='C'
  TEMPLATE template0;
GRANT ALL PRIVILEGES ON DATABASE synapse_db TO synapse;

-- Create Human Index database and user
CREATE USER human_index WITH PASSWORD 'CHANGE_ME_HUMAN_INDEX';
CREATE DATABASE human_index_db OWNER human_index;
GRANT ALL PRIVILEGES ON DATABASE human_index_db TO human_index;

-- Create Spark Swarm database and user
CREATE USER spark_swarm WITH PASSWORD 'CHANGE_ME_SPARK_SWARM';
CREATE DATABASE spark_swarm_db OWNER spark_swarm;
GRANT ALL PRIVILEGES ON DATABASE spark_swarm_db TO spark_swarm;

-- Create Esher's Codex database and user
CREATE USER eshers_codex WITH PASSWORD 'CHANGE_ME_ESHERS_CODEX';
CREATE DATABASE eshers_codex_db OWNER eshers_codex;
GRANT ALL PRIVILEGES ON DATABASE eshers_codex_db TO eshers_codex;

-- Create Noodle database and user
CREATE USER noodle WITH PASSWORD 'CHANGE_ME_NOODLE';
CREATE DATABASE noodle_db OWNER noodle;
GRANT ALL PRIVILEGES ON DATABASE noodle_db TO noodle;

CREATE USER for_whenever WITH PASSWORD '${FOR_WHENEVER_DB_PASSWORD}';
CREATE DATABASE for_whenever_db OWNER for_whenever;
GRANT ALL PRIVILEGES ON DATABASE for_whenever_db TO for_whenever;
