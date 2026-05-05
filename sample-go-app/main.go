// Sample guest application that implements Lambda MicroVMs lifecycle hooks.
//
// Listens on port 8080. /execute interprets Go source via the yaegi
// interpreter (a fresh interpreter per request, matching the Python sample's
// stateless snippet-runner semantics).
package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"sync"
	"time"

	"github.com/traefik/yaegi/interp"
	"github.com/traefik/yaegi/stdlib"
)

const (
	basePath = "/aws/lambda-microvms/runtime/beta/v1"
	port     = 8080
)

var (
	microVmIDMu sync.RWMutex
	microVmID   string
)

type launchRequest struct {
	MicroVmID       string `json:"microVmId"`
	MeshIpv6Address string `json:"meshIpv6Address"`
}

type executeRequest struct {
	Code string `json:"code"`
}

func main() {
	logf("Starting sample guest application on port %d", port)

	mux := http.NewServeMux()
	mux.HandleFunc("/health", health)
	mux.HandleFunc(basePath+"/ready", emptyHook("Ready"))
	mux.HandleFunc(basePath+"/launch", launch)
	mux.HandleFunc(basePath+"/resume", emptyHook("Resume"))
	mux.HandleFunc(basePath+"/suspend", emptyHook("Suspend"))
	mux.HandleFunc(basePath+"/terminate", emptyHook("Terminate"))
	mux.HandleFunc("/execute", execute)

	printSampleCommands()
	addr := fmt.Sprintf("0.0.0.0:%d", port)
	if err := http.ListenAndServe(addr, mux); err != nil {
		log.Fatal(err)
	}
}

func health(w http.ResponseWriter, r *http.Request) {
	logf("Health check called [ts=%s, microVmId=%s]", nowTs(), getMicroVmID())
	writeJSON(w, http.StatusOK, map[string]string{"status": "healthy"})
}

func emptyHook(name string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		logf("%s hook called [ts=%s, microVmId=%s]", name, nowTs(), getMicroVmID())
		w.WriteHeader(http.StatusOK)
	}
}

func launch(w http.ResponseWriter, r *http.Request) {
	var req launchRequest
	_ = json.NewDecoder(r.Body).Decode(&req)
	microVmIDMu.Lock()
	microVmID = req.MicroVmID
	microVmIDMu.Unlock()
	logf("Launch hook called — ts=%s, microVmId=%s, meshIpv6Address=%s",
		nowTs(), req.MicroVmID, req.MeshIpv6Address)
	w.WriteHeader(http.StatusOK)
}

func execute(w http.ResponseWriter, r *http.Request) {
	var req executeRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.Code == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "No code provided"})
		return
	}

	logf("Execute called [ts=%s, microVmId=%s]", nowTs(), getMicroVmID())

	var stdout, stderr bytes.Buffer
	i := interp.New(interp.Options{Stdout: &stdout, Stderr: &stderr})
	if err := i.Use(stdlib.Symbols); err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}

	if _, err := i.Eval(req.Code); err != nil {
		writeJSON(w, http.StatusOK, map[string]interface{}{
			"success": false,
			"error":   err.Error(),
			"stderr":  stderr.String(),
		})
		return
	}

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"success": true,
		"output":  stdout.String(),
		"stderr":  stderr.String(),
	})
}

func writeJSON(w http.ResponseWriter, status int, body interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}

func nowTs() string {
	return time.Now().UTC().Format(time.RFC3339Nano)
}

func logf(format string, args ...interface{}) {
	log.Printf("[sample-go-app] INFO - "+format, args...)
}

func getMicroVmID() string {
	microVmIDMu.RLock()
	defer microVmIDMu.RUnlock()
	return microVmID
}

func printSampleCommands() {
	fmt.Printf(`
Sample commands (server running on port %d):

  curl http://127.0.0.1:%d/health

  curl -X POST http://127.0.0.1:%d%s/ready

  curl -X POST http://127.0.0.1:%d%s/launch \
    -H 'Content-Type: application/json' \
    -d '{"microVmId": "hello_world", "meshIpv6Address": "::1"}'

  curl -X POST http://127.0.0.1:%d%s/resume
  curl -X POST http://127.0.0.1:%d%s/suspend
  curl -X POST http://127.0.0.1:%d%s/terminate

  curl -X POST http://127.0.0.1:%d/execute \
    -H 'Content-Type: application/json' \
    -d '{"code": "fmt.Println(1 + 1)"}'

`, port, port, port, basePath, port, basePath, port, basePath,
		port, basePath, port, basePath, port)
}
