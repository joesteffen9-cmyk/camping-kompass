<?php
declare(strict_types=1);

header('Content-Type: application/json; charset=utf-8');
header('X-Content-Type-Options: nosniff');
header('Cache-Control: no-store, no-cache, must-revalidate, max-age=0');

// Geschuetzte Konfiguration ausserhalb von public_html.
$configFile = dirname(__DIR__) . '/camping_config/fragen-config.php';
if (!is_file($configFile) || !is_readable($configFile)) {
    http_response_code(500);
    echo json_encode(['ok' => false, 'message' => 'Die geschuetzte Serverkonfiguration fehlt.'], JSON_UNESCAPED_UNICODE);
    exit;
}
$config = require $configFile;
if (!is_array($config) ||
    !isset($config['db_host'], $config['db_name'], $config['db_user'], $config['db_pass'], $config['admin_password_hash'])) {
    http_response_code(500);
    echo json_encode(['ok' => false, 'message' => 'Die Serverkonfiguration ist unvollstaendig.'], JSON_UNESCAPED_UNICODE);
    exit;
}

function respond(array $data, int $status = 200): never {
    http_response_code($status);
    echo json_encode($data, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
    exit;
}
function db(): PDO {
    static $pdo = null;
    if ($pdo instanceof PDO) return $pdo;
    $pdo = new PDO(
        'mysql:host=' . $GLOBALS['config']['db_host'] . ';dbname=' . $GLOBALS['config']['db_name'] . ';charset=utf8mb4',
        $GLOBALS['config']['db_user'],
        $GLOBALS['config']['db_pass'],
        [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
         PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
         PDO::ATTR_EMULATE_PREPARES => false]
    );
    return $pdo;
}
function body(): array {
    $value = json_decode(file_get_contents('php://input') ?: '{}', true);
    return is_array($value) ? $value : [];
}
function clean_text(mixed $value, int $max): string {
    $text = trim((string) $value);
    $text = preg_replace('/[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]/u', '', $text) ?? '';
    return mb_substr($text, 0, $max, 'UTF-8');
}
function require_admin(): void {
    if (empty($_SESSION['qa_admin'])) respond(['ok' => false, 'message' => 'Bitte zuerst als Betreiber anmelden.'], 401);
}

$secure = !empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off';
session_name('campingkompass_qa');
session_set_cookie_params([
    'lifetime' => 0, 'path' => '/', 'secure' => $secure,
    'httponly' => true, 'samesite' => 'Strict'
]);
session_start();

if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    respond(['ok' => false, 'message' => 'Methode nicht erlaubt.'], 405);
}
$in = body();
$action = (string) ($in['action'] ?? '');

try {
    if ($action === 'public') {
        $stmt = db()->query("SELECT id, '' AS place, frage AS question, antwort AS answer FROM camping_fragen WHERE status = 'beantwortet' ORDER BY id DESC");
        respond(['ok' => true, 'questions' => $stmt->fetchAll()]);
    }
    if ($action === 'ask') {
        if (!empty($in['website'])) respond(['ok' => true]);
        if (isset($_SESSION['qa_last_submit']) && time() - (int) $_SESSION['qa_last_submit'] < 45) {
            respond(['ok' => false, 'message' => 'Bitte warte kurz, bevor du eine weitere Frage sendest.'], 429);
        }
        $name = clean_text($in['author'] ?? $in['name'] ?? '', 100);
        $question = clean_text($in['question'] ?? $in['frage'] ?? '', 1500);
        if (mb_strlen($question, 'UTF-8') < 8) {
            respond(['ok' => false, 'message' => 'Bitte formuliere eine etwas ausführlichere Frage.'], 422);
        }
        $stmt = db()->prepare("INSERT INTO camping_fragen (name, frage, status) VALUES (?, ?, 'offen')");
        $stmt->execute([$name, $question]);
        $_SESSION['qa_last_submit'] = time();
        respond(['ok' => true]);
    }
    if ($action === 'login') {
        if (!password_verify((string) ($in['password'] ?? ''), (string) $config['admin_password_hash'])) {
            respond(['ok' => false, 'message' => 'Der Zahlencode stimmt nicht oder wurde noch nicht eingerichtet.'], 401);
        }
        session_regenerate_id(true);
        $_SESSION['qa_admin'] = true;
        // CSRF-Schutz fuer administrative Schreibaktionen.
        $_SESSION['qa_csrf'] = bin2hex(random_bytes(32));
        respond(['ok' => true, 'csrf_token' => $_SESSION['qa_csrf']]);
    }
    if ($action === 'admin_list') {
        require_admin();
        $stmt = db()->query("SELECT id, '' AS place, name AS author, frage AS question, erstellt_am AS created_at FROM camping_fragen WHERE status = 'offen' ORDER BY id ASC");
        respond(['ok' => true, 'questions' => $stmt->fetchAll(), 'csrf_token' => $_SESSION['qa_csrf'] ?? null]);
    }
    if ($action === 'admin_all') {
        require_admin();
        $stmt = db()->query("SELECT id, '' AS place, name AS author, frage AS question, antwort AS answer, status, erstellt_am AS created_at FROM camping_fragen ORDER BY id DESC");
        respond(['ok' => true, 'questions' => $stmt->fetchAll(), 'csrf_token' => $_SESSION['qa_csrf'] ?? null]);
    }
    if ($action === 'answer') {
        require_admin();
        $id = filter_var($in['id'] ?? null, FILTER_VALIDATE_INT);
        $answer = clean_text($in['answer'] ?? $in['antwort'] ?? '', 3000);
        if (!$id || mb_strlen($answer, 'UTF-8') < 2) {
            respond(['ok' => false, 'message' => 'Frage oder Antwort ist ungültig.'], 422);
        }
        $stmt = db()->prepare("UPDATE camping_fragen SET antwort = ?, status = 'beantwortet' WHERE id = ? AND status = 'offen'");
        $stmt->execute([$answer, $id]);
        if (!$stmt->rowCount()) {
            respond(['ok' => false, 'message' => 'Diese Frage wurde inzwischen bereits bearbeitet.'], 409);
        }
        respond(['ok' => true]);
    }
    if ($action === 'delete') {
        require_admin();
        $expected = (string) ($_SESSION['qa_csrf'] ?? '');
        $provided = (string) ($in['csrf_token'] ?? '');
        if ($expected === '' || $provided === '' || !hash_equals($expected, $provided)) {
            respond(['ok' => false, 'message' => 'Sicherheitspruefung fehlgeschlagen. Bitte erneut anmelden.'], 403);
        }
        $id = filter_var($in['id'] ?? null, FILTER_VALIDATE_INT, ['options' => ['min_range' => 1]]);
        if ($id === false || $id === null) {
            respond(['ok' => false, 'message' => 'Ungueltige Fragen-ID.'], 422);
        }
        $stmt = db()->prepare('DELETE FROM camping_fragen WHERE id = ?');
        $stmt->execute([$id]);
        if ($stmt->rowCount() === 0) {
            respond(['ok' => false, 'message' => 'Frage nicht gefunden.'], 404);
        }
        respond(['ok' => true, 'message' => 'Frage und zugehoerige Antwort wurden geloescht.']);
    }
    if ($action === 'logout') {
        $_SESSION = [];
        if (ini_get('session.use_cookies')) {
            $p = session_get_cookie_params();
            setcookie(session_name(), '', [
                'expires' => time() - 42000, 'path' => $p['path'],
                'domain' => $p['domain'] ?? '', 'secure' => $p['secure'],
                'httponly' => $p['httponly'], 'samesite' => 'Strict'
            ]);
        }
        session_destroy();
        respond(['ok' => true]);
    }
    respond(['ok' => false, 'message' => 'Unbekannte Aktion.'], 400);
} catch (Throwable $e) {
    error_log('Camping-Kompass Q&A: ' . $e->getMessage());
    respond(['ok' => false, 'message' => 'Der Fragenbereich ist momentan nicht erreichbar. Bitte Datenbankverbindung prüfen.'], 500);
}
