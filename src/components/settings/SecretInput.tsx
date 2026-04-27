import { useState } from "react";
import { Eye, EyeOff } from "lucide-react";

interface SecretInputProps {
  placeholder?: string;
  value: string;
  onChange: (value: string) => void;
}

export function SecretInput(props: SecretInputProps) {
  const { placeholder, value, onChange } = props;
  const [visible, setVisible] = useState(false);
  const Icon = visible ? EyeOff : Eye;

  return (
    <div className="secret-input-wrap">
      <input
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        type={visible ? "text" : "password"}
        value={value}
      />
      <button
        aria-label={visible ? "隐藏明文" : "显示明文"}
        className="secret-toggle-button"
        onClick={() => setVisible((current) => !current)}
        title={visible ? "隐藏明文" : "显示明文"}
        type="button"
      >
        <Icon size={16} />
      </button>
    </div>
  );
}
