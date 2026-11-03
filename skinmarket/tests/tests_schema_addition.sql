-- Add to skinmarket/tests/schema_sqlite.sql after Пользователи is created.
CREATE TABLE SteamAccounts (
  SteamID TEXT(20) PRIMARY KEY,
  КодПользователя INTEGER NOT NULL UNIQUE REFERENCES Пользователи(КодПользователя),
  ДатаПривязки TEXT
);
