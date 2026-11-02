-- Тестовая копия схемы SKINMARKET для SQLite (боевая база — Access, см. database.accdb)
CREATE TABLE Роли (КодРоли INTEGER PRIMARY KEY AUTOINCREMENT, НазваниеРоли TEXT NOT NULL UNIQUE);
CREATE TABLE Игры (КодИгры INTEGER PRIMARY KEY AUTOINCREMENT, НазваниеИгры TEXT NOT NULL UNIQUE);
CREATE TABLE Редкости (КодРедкости INTEGER PRIMARY KEY AUTOINCREMENT, НазваниеРедкости TEXT NOT NULL UNIQUE);
CREATE TABLE СтатусыОбъявлений (КодСтатуса INTEGER PRIMARY KEY AUTOINCREMENT, НазваниеСтатуса TEXT NOT NULL UNIQUE);
CREATE TABLE СтатусыЗаказов (КодСтатуса INTEGER PRIMARY KEY AUTOINCREMENT, НазваниеСтатуса TEXT NOT NULL UNIQUE);
CREATE TABLE Пользователи (
  КодПользователя INTEGER PRIMARY KEY AUTOINCREMENT, Логин TEXT NOT NULL UNIQUE, Email TEXT NOT NULL UNIQUE,
  ХешПароля TEXT NOT NULL, КодРоли INTEGER NOT NULL REFERENCES Роли(КодРоли), Баланс REAL DEFAULT 0,
  ДатаРегистрации TEXT DEFAULT CURRENT_TIMESTAMP, Заблокирован INTEGER DEFAULT 0);
CREATE TABLE Категории (
  КодКатегории INTEGER PRIMARY KEY AUTOINCREMENT, КодИгры INTEGER NOT NULL REFERENCES Игры(КодИгры),
  НазваниеКатегории TEXT NOT NULL);
CREATE TABLE Товары (
  КодТовара INTEGER PRIMARY KEY AUTOINCREMENT, КодИгры INTEGER NOT NULL REFERENCES Игры(КодИгры),
  КодКатегории INTEGER NOT NULL REFERENCES Категории(КодКатегории),
  КодРедкости INTEGER NOT NULL REFERENCES Редкости(КодРедкости),
  Название TEXT NOT NULL, Описание TEXT, URLИзображения TEXT);
CREATE TABLE Объявления (
  КодОбъявления INTEGER PRIMARY KEY AUTOINCREMENT, КодПродавца INTEGER NOT NULL REFERENCES Пользователи(КодПользователя),
  КодТовара INTEGER NOT NULL REFERENCES Товары(КодТовара), Цена REAL NOT NULL CHECK (Цена > 0),
  Количество INTEGER NOT NULL DEFAULT 1 CHECK (Количество >= 0),
  КодСтатуса INTEGER NOT NULL REFERENCES СтатусыОбъявлений(КодСтатуса), ДатаСоздания TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE Заказы (
  КодЗаказа INTEGER PRIMARY KEY AUTOINCREMENT, КодПокупателя INTEGER NOT NULL REFERENCES Пользователи(КодПользователя),
  КодОбъявления INTEGER NOT NULL REFERENCES Объявления(КодОбъявления), Количество INTEGER NOT NULL DEFAULT 1 CHECK (Количество > 0),
  Сумма REAL NOT NULL, ДатаЗаказа TEXT DEFAULT CURRENT_TIMESTAMP,
  КодСтатуса INTEGER NOT NULL REFERENCES СтатусыЗаказов(КодСтатуса));
CREATE TABLE Транзакции (
  КодТранзакции INTEGER PRIMARY KEY AUTOINCREMENT, КодПользователя INTEGER NOT NULL REFERENCES Пользователи(КодПользователя),
  КодЗаказа INTEGER REFERENCES Заказы(КодЗаказа), Тип TEXT NOT NULL, Сумма REAL NOT NULL, Дата TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE Отзывы (
  КодОтзыва INTEGER PRIMARY KEY AUTOINCREMENT, КодЗаказа INTEGER NOT NULL UNIQUE REFERENCES Заказы(КодЗаказа),
  Оценка INTEGER NOT NULL CHECK (Оценка >= 1 AND Оценка <= 5), Комментарий TEXT, ДатаОтзыва TEXT DEFAULT CURRENT_TIMESTAMP);

INSERT INTO Роли (НазваниеРоли) VALUES ('Покупатель'), ('Продавец'), ('Администратор');
INSERT INTO СтатусыОбъявлений (НазваниеСтатуса) VALUES ('Активно'), ('Продано'), ('Снято');
INSERT INTO СтатусыЗаказов (НазваниеСтатуса) VALUES ('Создан'), ('Оплачен'), ('Выполнен'), ('Отменён');
INSERT INTO Редкости (НазваниеРедкости) VALUES ('Обычное'), ('Редкое'), ('Эпическое'), ('Легендарное');
INSERT INTO Игры (НазваниеИгры) VALUES ('CS2');
INSERT INTO Категории (КодИгры, НазваниеКатегории) VALUES (1, 'Винтовки');
