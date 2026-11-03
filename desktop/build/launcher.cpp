// DropLink 互传 —— 自解压启动器
//
// 编译后的 EXE 尾部附加了一整段 payload（Python 运行时 + 应用代码）。
// 运行逻辑：
//   1. 从自身尾部定位 payload；
//   2. 释放到 %LOCALAPPDATA%\DropLink\runtime（已存在且大小一致则跳过）；
//   3. CreateProcess 拉起 pythonw.exe 运行 app_main.py（无黑色控制台窗口）；
//   4. 等待子进程结束。
//
// 全程无外部依赖，静态链接。

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <shlobj.h>
#include <stdio.h>
#include <string>
#include <vector>

#ifndef MAX_PATH_W
#define MAX_PATH_W 4096
#endif

static const char MAGIC[8] = {'D', 'L', 'P', 'A', 'Y', '0', '0', '1'};

static std::wstring g_exePath;
static std::wstring g_cacheDir;

static void fail(const wchar_t *msg) {
    MessageBoxW(NULL, msg, L"DropLink 互传", MB_ICONERROR | MB_OK);
}

static std::wstring dirOf(const std::wstring &p) {
    size_t i = p.find_last_of(L"\\/");
    return (i == std::wstring::npos) ? std::wstring(L".") : p.substr(0, i);
}

static bool readU64(FILE *f, unsigned long long &v) {
    unsigned char b[8];
    if (fread(b, 1, 8, f) != 8) return false;
    v = 0;
    for (int i = 7; i >= 0; --i) v = (v << 8) | b[i];
    return true;
}

static bool readU32(FILE *f, unsigned int &v) {
    unsigned char b[4];
    if (fread(b, 1, 4, f) != 4) return false;
    v = ((unsigned)b[0]) | ((unsigned)b[1] << 8) | ((unsigned)b[2] << 16) | ((unsigned)b[3] << 24);
    return true;
}

static std::wstring utf8ToWide(const std::string &s) {
    if (s.empty()) return std::wstring();
    int n = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), (int)s.size(), NULL, 0);
    std::wstring out((size_t)n, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.c_str(), (int)s.size(), &out[0], n);
    return out;
}

static void ensureDir(const std::wstring &d) {
    CreateDirectoryW(d.c_str(), NULL);
}

static void makeParentDirs(const std::wstring &full) {
    std::wstring d = dirOf(full);
    // 逐级创建
    std::wstring cur;
    for (size_t i = 0; i < d.size(); ++i) {
        wchar_t c = d[i];
        if (c == L'\\' || c == L'/') {
            if (cur.size() > 2) CreateDirectoryW(cur.c_str(), NULL);
        }
        cur += c;
    }
    if (cur.size() > 2) CreateDirectoryW(cur.c_str(), NULL);
}

struct Entry {
    std::wstring rel;
    long long off;
    unsigned long long size;
};

static bool readWhole(FILE *f, long long off, unsigned long long n, std::string &out) {
    _fseeki64(f, off, SEEK_SET);
    out.assign((size_t)n, '\0');
    if (n == 0) return true;
    return fread(&out[0], 1, (size_t)n, f) == n;
}

// 释放 payload，返回 true 表示成功
static bool extractPayload() {
    FILE *f = _wfopen(g_exePath.c_str(), L"rb");
    if (!f) {
        fail(L"无法读取自身文件。");
        return false;
    }
    _fseeki64(f, 0, SEEK_END);
    long long total = _ftelli64(f);

    if (total < 32) {
        fclose(f);
        fail(L"安装包已损坏（文件过小）。");
        return false;
    }
    _fseeki64(f, total - 16, SEEK_SET);
    char magic[8];
    if (fread(magic, 1, 8, f) != 8) { fclose(f); fail(L"安装包已损坏。"); return false; }
    unsigned long long psize = 0;
    if (!readU64(f, psize)) { fclose(f); fail(L"安装包已损坏。"); return false; }
    if (memcmp(magic, MAGIC, 8) != 0 || psize == 0 || psize > (unsigned long long)(total - 16)) {
        fclose(f);
        fail(L"安装包已损坏（尾部标记无效）。");
        return false;
    }

    long long pstart = total - 16 - (long long)psize;

    // 第一遍：只扫描文件表
    std::vector<Entry> ents;
    long long pos = pstart;
    long long end = pstart + (long long)psize;
    _fseeki64(f, pstart, SEEK_SET);
    while (pos + 12 <= end) {
        unsigned int nlen = 0;
        if (!readU32(f, nlen)) break;
        std::string name(nlen, '\0');
        if (nlen && fread(&name[0], 1, nlen, f) != nlen) break;
        unsigned long long dsize = 0;
        if (!readU64(f, dsize)) break;
        pos += 4 + nlen + 8;
        Entry e;
        e.rel = utf8ToWide(name);
        for (size_t i = 0; i < e.rel.size(); ++i)
            if (e.rel[i] == L'/') e.rel[i] = L'\\';
        e.off = pos;
        e.size = dsize;
        ents.push_back(e);
        _fseeki64(f, pos + (long long)dsize, SEEK_SET);
        pos += (long long)dsize;
    }
    if (ents.empty()) { fclose(f); fail(L"安装包已损坏（没有内容）。"); return false; }

    ensureDir(g_cacheDir);

    // 构建号不同 -> 全部覆盖，避免"改了代码但大小没变"导致不更新
    bool force = false;
    std::string newId, oldId;
    for (size_t i = 0; i < ents.size(); ++i) {
        if (ents[i].rel == L".buildid") {
            readWhole(f, ents[i].off, ents[i].size, newId);
            break;
        }
    }
    FILE *idf = _wfopen((g_cacheDir + L"\\.buildid").c_str(), L"rb");
    if (idf) {
        char b[256];
        size_t n = fread(b, 1, sizeof(b), idf);
        oldId.assign(b, n);
        fclose(idf);
    }
    while (!newId.empty() && (newId.back() == '\n' || newId.back() == '\r')) newId.pop_back();
    while (!oldId.empty() && (oldId.back() == '\n' || oldId.back() == '\r')) oldId.pop_back();
    if (!newId.empty() && newId != oldId) force = true;

    for (size_t i = 0; i < ents.size(); ++i) {
        const Entry &e = ents[i];
        std::wstring full = g_cacheDir + L"\\" + e.rel;

        bool skip = false;
        if (!force) {
            WIN32_FILE_ATTRIBUTE_DATA ad;
            if (GetFileAttributesExW(full.c_str(), GetFileExInfoStandard, &ad)) {
                unsigned long long have =
                    ((unsigned long long)ad.nFileSizeHigh << 32) | ad.nFileSizeLow;
                if (have == e.size && !(ad.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY))
                    skip = true;
            }
        }
        if (skip) continue;

        makeParentDirs(full);
        FILE *o = _wfopen(full.c_str(), L"wb");
        if (!o) { fclose(f); fail(L"无法写入运行目录。"); return false; }
        _fseeki64(f, e.off, SEEK_SET);
        const size_t CH = 1 << 20;
        std::vector<char> buf(CH);
        unsigned long long left = e.size;
        while (left > 0) {
            size_t want = (left > CH) ? CH : (size_t)left;
            size_t got = fread(&buf[0], 1, want, f);
            if (got == 0) break;
            fwrite(&buf[0], 1, got, o);
            left -= got;
        }
        fclose(o);
    }
    fclose(f);
    return true;
}

static bool getCacheDir() {
    wchar_t path[MAX_PATH_W];
    if (FAILED(SHGetFolderPathW(NULL, CSIDL_LOCAL_APPDATA, NULL, 0, path))) {
        if (!GetEnvironmentVariableW(L"LOCALAPPDATA", path, MAX_PATH_W))
            wcscpy_s(path, L"C:\\Windows\\Temp");
    }
    g_cacheDir = std::wstring(path) + L"\\DropLink\\runtime";
    return true;
}

static void appendArg(std::wstring &cmd, const std::wstring &a) {
    cmd += L" \"";
    for (size_t i = 0; i < a.size(); ++i) {
        if (a[i] == L'"') cmd += L"\\\"";
        else cmd += a[i];
    }
    cmd += L"\"";
}

static bool fileExists(const wchar_t *p) {
    return GetFileAttributesW(p) != INVALID_FILE_ATTRIBUTES;
}

static std::wstring findBrowser() {
    wchar_t pfx[300] = {0}, pf[300] = {0};
    GetEnvironmentVariableW(L"ProgramFiles(x86)", pfx, 300);
    GetEnvironmentVariableW(L"ProgramFiles", pf, 300);
    std::wstring cands[4] = {
        std::wstring(pfx) + L"\\Microsoft\\Edge\\Application\\msedge.exe",
        std::wstring(pf) + L"\\Microsoft\\Edge\\Application\\msedge.exe",
        std::wstring(pf) + L"\\Google\\Chrome\\Application\\chrome.exe",
        std::wstring(pfx) + L"\\Google\\Chrome\\Application\\chrome.exe",
    };
    for (int i = 0; i < 4; ++i)
        if (fileExists(cands[i].c_str())) return cands[i];
    return std::wstring();
}

// 读运行目录里的 .port，得到服务实际端口
static int readPort() {
    std::wstring p = g_cacheDir + L"\\.port";
    FILE *f = _wfopen(p.c_str(), L"r");
    if (!f) return 0;
    wchar_t line[64] = {0};
    if (!fgetws(line, 64, f)) { fclose(f); return 0; }
    fclose(f);
    return _wtoi(line);
}

// 以应用模式重新打开电脑端界面
static bool launchApp(int port) {
    std::wstring exe = findBrowser();
    if (exe.empty()) return false;
    wchar_t url[160];
    swprintf(url, 160, L"http://127.0.0.1:%d/#about", port);
    std::wstring line = L"\"" + exe + L"\" --app=" + url;
    std::vector<wchar_t> buf(line.begin(), line.end());
    buf.push_back(0);
    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    ZeroMemory(&si, sizeof(si)); si.cb = sizeof(si);
    ZeroMemory(&pi, sizeof(pi));
    if (!CreateProcessW(exe.c_str(), &buf[0], NULL, NULL, FALSE, 0, NULL, NULL, &si, &pi))
        return false;
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return true;
}

int WINAPI wWinMain(HINSTANCE, HINSTANCE, PWSTR cmdLine, int) {
    // 单实例：已经在跑时，把界面窗口置顶（而不是再开一个）
    HANDLE mx = CreateMutexW(NULL, FALSE, L"DropLinkSingleton_1");
    if (mx && GetLastError() == ERROR_ALREADY_EXISTS) {
        // 界面窗口标题 = manifest 里的应用名
        HWND w = FindWindowW(NULL, L"DropLink 互传");
        if (w) {
            if (IsIconic(w)) ShowWindow(w, SW_RESTORE);
            SetForegroundWindow(w);
            return 0;
        }
        // 界面窗口已被关掉 → 按记录下的端口重新打开
        int port = readPort();
        if (port > 0 && launchApp(port)) {
            return 0;
        }
        MessageBoxW(NULL, L"DropLink 互传 已经在运行了（看右下角托盘图标）。",
                    L"DropLink 互传", MB_ICONINFORMATION | MB_OK);
        return 0;
    }

    wchar_t self[MAX_PATH_W];
    if (!GetModuleFileNameW(NULL, self, MAX_PATH_W)) {
        fail(L"无法定位程序路径。");
        return 1;
    }
    g_exePath = self;
    getCacheDir();

    if (!extractPayload()) return 1;

    std::wstring py = g_cacheDir + L"\\pythonw.exe";
    std::wstring script = g_cacheDir + L"\\app\\app_main.py";
    if (GetFileAttributesW(py.c_str()) == INVALID_FILE_ATTRIBUTES) {
        fail(L"运行目录缺少 pythonw.exe，请重新下载本程序。");
        return 1;
    }

    // 命令行参数原样传给 app_main.py
    std::wstring cmd = py;
    appendArg(cmd, script);
    if (cmdLine && *cmdLine) {
        // 简单按空格切分（本程序自身参数很少）
        std::wstring cur;
        for (const wchar_t *p = cmdLine; *p; ++p) {
            if (*p == L' ') {
                if (!cur.empty()) { appendArg(cmd, cur); cur.clear(); }
            } else {
                cur += *p;
            }
        }
        if (!cur.empty()) appendArg(cmd, cur);
    }

    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    ZeroMemory(&si, sizeof(si));
    ZeroMemory(&pi, sizeof(pi));
    si.cb = sizeof(si);

    std::vector<wchar_t> buf(cmd.begin(), cmd.end());
    buf.push_back(0);

    if (!CreateProcessW(py.c_str(), &buf[0], NULL, NULL, FALSE, 0, NULL,
                        g_cacheDir.c_str(), &si, &pi)) {
        fail(L"启动失败，可能是杀毒软件拦截了释放出的运行时。");
        return 1;
    }
    CloseHandle(pi.hThread);
    WaitForSingleObject(pi.hProcess, INFINITE);
    DWORD code = 0;
    GetExitCodeProcess(pi.hProcess, &code);
    CloseHandle(pi.hProcess);
    return (int)code;
}
