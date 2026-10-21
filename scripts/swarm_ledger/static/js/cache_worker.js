self.onmessage = ({ data }) => {
  self.postMessage({ version: data.version, value: JSON.stringify(data.value) });
};
