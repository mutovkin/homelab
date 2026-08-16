-- Create databases for different applications
CREATE DATABASE joplin;
--CREATE DATABASE nextcloud;
--CREATE DATABASE gitea;
--CREATE DATABASE photoprism;

-- Create users with appropriate permissions
CREATE USER joplin_user WITH PASSWORD 'joplin_secure_password';
--CREATE USER nextcloud_user WITH PASSWORD 'nextcloud_secure_password';
--CREATE USER gitea_user WITH PASSWORD 'gitea_secure_password';
--CREATE USER photoprism_user WITH PASSWORD 'photoprism_secure_password';

-- Grant permissions
GRANT ALL PRIVILEGES ON DATABASE joplin TO joplin_user;
--GRANT ALL PRIVILEGES ON DATABASE nextcloud TO nextcloud_user;
--GRANT ALL PRIVILEGES ON DATABASE gitea TO gitea_user;
--GRANT ALL PRIVILEGES ON DATABASE photoprism TO photoprism_user;