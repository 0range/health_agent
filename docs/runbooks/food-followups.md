# Завтрак и уточнения еды

Обновление от 10 сентября 2026.

Утреннее напоминание о завтраке настроено на **09:00 по Москве**. Оно приходит один
раз за дату, если завтрак ещё не записан и после назначенного времени не записан
другой приём пищи. После сбоя доставки попытка повторяется; успешная доставка
подавляет повтор после перезапуска. Окно доставки — два часа, поэтому пропущенное
утром напоминание не придёт после обеда. Общая пауза и тихие часы действуют и здесь.

Команды в фудботе:

- `/завтрак 09:30` — изменить время и включить утреннее напоминание;
- `/завтрак выкл` / `/завтрак вкл` — отключить/включить только завтрак;
- `/напоминания выкл` / `/напоминания вкл` — общая пауза/включение.

Напоминания зависят от работающего Mac, Docker и службы фудбота. Интервал до
следующей еды по-прежнему считается от последнего фото + 20 минут; обычный
комментарий его не сдвигает. После ужина бот показывает настройку завтрака.

## Дополнения к приёму пищи

Короткое «Хлеб добавлю» сохраняется как план и явно отражается в ответе. Бот не
повторяет предложение добавить компонент, который уже запланирован, и не считает
план съеденным. «Добавил хлеб» подтверждает дополнение к текущему приёму, при этом
исходный комментарий остаётся в истории. При подтверждении с указанной порцией,
например «Добавил ломтик хлеба», соответствующий план тоже закрывается.

Модель получает подтверждённые и планируемые дополнения отдельно. Подтверждённый
продукт сохраняется даже если модель снова его пропустила; в таком случае прежние
нутриенты обнуляются до `null` (не до нулевых калорий), поскольку они не учитывали
дополнение. При успешном повторном разборе оценивается весь исправленный приём.
Вес и калорийность остаются приблизительными, если порция не измерена.

Детерминированное распознавание ограничено однозначными короткими фразами с
«добавлю», «добавил», «добавляю» и их отрицаниями. Вопросы и условные фразы не
подтверждают еду. Остальные уточнения продолжает разбирать модель; это не обещание
безошибочного понимания произвольной переписки.

В ответах модели одиночная строка `foods` или `unknowns` теперь сохраняется как
один элемент списка. Это восстанавливает состав блюда и оговорки о размере порций,
которые раньше терялись при несовпадении формата. Исходный JSON сохраняется.

## Проверка

241 тест пилотных ботов пройден, Ruff и mypy для изменённых модулей проходят.
Проверены утро после ужина, отдельная/общая пауза, тихие часы, повтор доставки,
перезапуск, уже записанный завтрак, истечение окна, планы/подтверждения, пропуск
продукта моделью, недоступный анализ и нормализация строкового состава.

Реальные записи исправлены с приватной резервной копией исходных payload; обед
переразобран по новому подтверждению владельца. История Telegram и старые ответы не
переписаны, дополнительных сообщений от имени пользователя не отправлено. Служба
фудбота перезапущена, опрос Telegram свежий и без ошибки. Проверка следующего утра
выполнена без отправки тестового напоминания и без создания выдуманной еды.

## Meal continuity and short weekly advice

A food description following a delivered, unanswered breakfast/meal notice is
captured without asking whether it is a new meal. Explicit consumed-meal verbs
(including `покушал/покушала`) create a new meal and restart the existing interval
calculation; additions/corrections stay with the current meal. Plans, refusals
and questions do not become eaten meals. Retry uses the original source key.
A superseded comment can be retained for audit with `superseded_by_meal`, while
being excluded from reanalysis of the old meal. Historical receipts are retained.

Carbohydrate source labels distinguish whole grains/legumes, refined starch,
free sugars, whole fruit and unspecified starch. This is ingredient evidence,
not measured sugar grams, glycemic index or a guarantee of slow absorption.
Weekly output selects one concrete action and one basis from logged meals in
at most 450 characters; insufficient evidence is explicit. `/неделя` uses the
new format immediately; previously delivered weekly messages are not resent.

## Repeated reminders (14 September 2026)

Each pending meal receives at most **three delivered notifications**: the first
at the existing target, then two repeats at least **30 minutes** after the
previous recorded delivery. This includes breakfast, lunch, afternoon snack and
dinner; logging dinner still ends that day's meal-interval reminders. The
notification names the next meal and asks the user to log food if already eaten.
It does not infer fasting from an absent journal entry.

The series uses successful `notice` receipts only, including legacy first-notice
keys. Outbound attempts do not count as delivery. Restart can send one overdue
notification, never a backlog at once. Repeats have bounded grace through 90
minutes after the first receipt (or the existing initial expiry, whichever is
later); quiet hours and the breakfast window still apply. The 4.5-hour interval
option has a 30-minute initial delivery window so polling jitter cannot lose it.

A newly recorded meal stops the old series. `/пропустить` stops the pending
series, `/напоминания выкл` pauses notices, and `/позже N` postpones the next
notice without resetting the total cap. An active breakfast series has its own
Moscow-date control, so postponing/skipping it works even without a prior meal.
Snooze retries preserve the original stored target and confirmation.

## Короткие дополнения: 14 сентября 2026

Поддерживаются «ещё …», «и ещё …», «а ещё …» и «+ …» с известными названиями
продуктов. Простой список разделяется на отдельные продукты; распространённые
уменьшительные формы сопоставляются с названиями в ответе модели. Планы, вопросы,
отрицания и просьбы рассказать о еде не подтверждают её употребление.

Если модель пропустила дополнение, бот сохраняет исправленный состав и один раз
повторяет расчёт без фотографии. Повтор должен описывать весь приём, включая
основное блюдо. При сбое или повторном пропуске продукты остаются в журнале,
а нутриенты неизвестны (`null`), пока пересчёт не удастся. Оба ответа сохраняются
для диагностики. Неизвестный размер порций остаётся оговоркой к оценке.

Однозначное уточнение вида «булгур это» заменяет единственную ранее распознанную
крупу. При нескольких крупах или составном блюде автоматическая замена не делается.
Дополнения и исправления явно показаны в ответе; повторная доставка того же
сообщения не создаёт ещё один приём и не сдвигает напоминание.

Проверка: 380 тестов пилота и Telegram, Ruff и mypy. Выполнено адресное исправление
двух записей с приватной резервной копией; остальные приёмы и время еды сохранены.
