(function () {
  var keyField = document.querySelector("input[name='idempotency_key']");
  if (keyField && keyField.form) {
    var remembered = sessionStorage.getItem("docia-idempotency");
    if (remembered) {
      keyField.value = remembered;
    } else {
      sessionStorage.setItem("docia-idempotency", keyField.value);
    }
  }
  if (document.body && document.querySelector("[data-clear-idempotency]")) {
    sessionStorage.removeItem("docia-idempotency");
  }

  var upload = document.querySelector("form[data-upload]");
  var progress = document.getElementById("upload-progress");
  if (upload && progress && window.FormData) {
    upload.addEventListener("submit", function (event) {
      if (!window.XMLHttpRequest) {
        return;
      }
      event.preventDefault();
      var body = new FormData(upload);
      var xhr = new XMLHttpRequest();
      xhr.open("POST", upload.action);
      xhr.setRequestHeader("Accept", "text/html");
      xhr.upload.addEventListener("progress", function (progressEvent) {
        if (!progressEvent.lengthComputable) {
          progress.textContent = "Enviando el archivo…";
          return;
        }
        var percent = Math.round((progressEvent.loaded / progressEvent.total) * 100);
        progress.textContent = "Envío del archivo: " + percent + " %. El procesamiento empezará después, si un revisor lo autoriza.";
      });
      xhr.addEventListener("load", function () {
        document.open();
        document.write(xhr.responseText);
        document.close();
      });
      xhr.send(body);
    });
  }

  var job = document.querySelector("[data-job-url]");
  if (!job) {
    return;
  }
  var url = job.getAttribute("data-job-url");
  var state = document.getElementById("job-state");
  var timer = null;
  function poll() {
    if (document.hidden) {
      return;
    }
    fetch(url, { headers: { Accept: "application/json" } })
      .then(function (response) { return response.json(); })
      .then(function (payload) {
        state.textContent = payload.state;
        if (payload.state === "queued" || payload.state === "running") {
          timer = window.setTimeout(poll, 3000);
        }
      })
      .catch(function () {
        timer = window.setTimeout(poll, 5000);
      });
  }
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden && !timer) {
      poll();
    }
  });
  poll();
})();
