"""
Jasypt 加解密工具类
完全兼容 Java Jasypt 的 StandardPBEStringEncryptor
可直接在其他模块中导入使用

使用示例:
    from jasypt_utils import JasyptEncryptor

    # 简单使用
    encrypted = JasyptEncryptor.encrypt('myPassword', 'plainText')
    decrypted = JasyptEncryptor.decrypt('myPassword', encrypted)

    # 或创建实例
    encryptor = JasyptEncryptor('myPassword')
    encrypted = encryptor.encrypt('plainText')
    decrypted = encryptor.decrypt(encrypted)
"""

from Crypto.Cipher import DES, DES3
from Crypto.Random import get_random_bytes
from Crypto.Util.Padding import pad, unpad
from Crypto.Hash import MD5, SHA1
import base64


class JasyptEncryptor:
    """
    Jasypt 加解密工具类
    完全兼容 Java Jasypt 的 StandardPBEStringEncryptor
    """

    # 支持的算法配置
    ALGORITHMS = {
        'PBEWithMD5AndDES': {
            'cipher': DES,
            'key_size': 8,
            'iv_size': 8,
            'hash_algo': MD5,
            'mode': DES.MODE_CBC
        },
        'PBEWithMD5AndTripleDES': {
            'cipher': DES3,
            'key_size': 24,
            'iv_size': 8,
            'hash_algo': MD5,
            'mode': DES3.MODE_CBC
        },
        'PBEWithSHA1AndDESede': {
            'cipher': DES3,
            'key_size': 24,
            'iv_size': 8,
            'hash_algo': SHA1,
            'mode': DES3.MODE_CBC
        }
    }

    # 默认算法
    DEFAULT_ALGORITHM = 'PBEWithMD5AndDES'

    # 默认迭代次数
    DEFAULT_ITERATIONS = 1000

    def __init__(self, password=None, algorithm=None, iterations=None):
        """
        初始化加密器

        Args:
            password: 加密密码
            algorithm: 加密算法，默认 'PBEWithMD5AndDES'
            iterations: 迭代次数，默认 1000
        """
        self.password = password
        self.algorithm = algorithm or self.DEFAULT_ALGORITHM
        self.iterations = iterations or self.DEFAULT_ITERATIONS

        if self.algorithm not in self.ALGORITHMS:
            raise ValueError(f"不支持的算法: {self.algorithm}. 支持的算法: {list(self.ALGORITHMS.keys())}")

    @staticmethod
    def _openssl_kdf(password, salt, key_size, iv_size, hash_algo, iterations=1):
        """
        OpenSSL 兼容的密钥派生函数 (EVP_BytesToKey)
        这是 Jasypt 实际使用的密钥派生方式

        Args:
            password: 密码 (bytes)
            salt: 盐值 (bytes)
            key_size: 密钥长度
            iv_size: IV 长度
            hash_algo: 哈希算法模块
            iterations: 迭代次数

        Returns:
            (key, iv): 密钥和初始化向量
        """
        m = []
        i = 0

        while len(b''.join(m)) < (key_size + iv_size):
            if i == 0:
                data = password + salt
            else:
                data = m[i - 1] + password + salt

            # 执行迭代哈希
            md = data
            for _ in range(iterations):
                md = hash_algo.new(md).digest()

            m.append(md)
            i += 1

        # 拼接所有哈希结果
        ms = b''.join(m)

        # 分离密钥和 IV
        key = ms[:key_size]
        iv = ms[key_size:key_size + iv_size]

        return key, iv

    def encrypt(self, plain_text, password=None, algorithm=None):
        """
        加密字符串

        Args:
            plain_text: 待加密的明文
            password: 加密密码（可选，使用实例密码）
            algorithm: 加密算法（可选，使用实例算法）

        Returns:
            Base64 编码的密文

        Raises:
            ValueError: 当密码未设置或算法不支持时
        """
        pwd = password or self.password
        algo = algorithm or self.algorithm

        if not pwd:
            raise ValueError("必须提供加密密码")

        if algo not in self.ALGORITHMS:
            raise ValueError(f"不支持的算法: {algo}")

        config = self.ALGORITHMS[algo]

        # 生成随机盐值 (8 字节)
        salt = get_random_bytes(8)

        # 使用 OpenSSL KDF 生成密钥和 IV
        key, iv = self._openssl_kdf(
            pwd.encode('utf-8'),
            salt,
            config['key_size'],
            config['iv_size'],
            config['hash_algo'],
            self.iterations
        )

        # 创建加密器
        cipher = config['cipher'].new(key, config['mode'], iv)

        # 填充并加密
        padded_data = pad(plain_text.encode('utf-8'), cipher.block_size)
        encrypted = cipher.encrypt(padded_data)

        # Jasypt 格式: salt(8字节) + ciphertext
        result = salt + encrypted
        return base64.b64encode(result).decode('utf-8')

    def decrypt(self, encrypted_text, password=None, algorithm=None):
        """
        解密字符串

        Args:
            encrypted_text: Base64 编码的密文
            password: 解密密码（可选，使用实例密码）
            algorithm: 解密算法（可选，使用实例算法）

        Returns:
            解密后的明文

        Raises:
            ValueError: 当密码未设置、算法不支持或解密失败时
        """
        pwd = password or self.password
        algo = algorithm or self.algorithm

        if not pwd:
            raise ValueError("必须提供解密密码")

        if algo not in self.ALGORITHMS:
            raise ValueError(f"不支持的算法: {algo}")

        config = self.ALGORITHMS[algo]

        try:
            # Base64 解码
            encrypted_bytes = base64.b64decode(encrypted_text)

            # 分离盐值和密文 (Jasypt 格式)
            salt = encrypted_bytes[:8]
            ciphertext = encrypted_bytes[8:]

            # 使用 OpenSSL KDF 生成密钥和 IV
            key, iv = self._openssl_kdf(
                pwd.encode('utf-8'),
                salt,
                config['key_size'],
                config['iv_size'],
                config['hash_algo'],
                self.iterations
            )

            # 创建解密器
            cipher = config['cipher'].new(key, config['mode'], iv)

            # 解密并去除填充
            decrypted = cipher.decrypt(ciphertext)
            unpadded = unpad(decrypted, cipher.block_size)

            return unpadded.decode('utf-8')

        except Exception as e:
            raise ValueError(f"解密失败: {str(e)}")

    @classmethod
    def encrypt_with_config(cls, plain_text, password, algorithm=None, iterations=None):
        """
        静态方法：使用指定配置加密

        Args:
            plain_text: 待加密的明文
            password: 加密密码
            algorithm: 加密算法（可选）
            iterations: 迭代次数（可选）

        Returns:
            Base64 编码的密文
        """
        encryptor = cls(password, algorithm, iterations)
        return encryptor.encrypt(plain_text)

    @classmethod
    def decrypt_with_config(cls, encrypted_text, password, algorithm=None, iterations=None):
        """
        静态方法：使用指定配置解密

        Args:
            encrypted_text: Base64 编码的密文
            password: 解密密码
            algorithm: 解密算法（可选）
            iterations: 迭代次数（可选）

        Returns:
            解密后的明文
        """
        encryptor = cls(password, algorithm, iterations)
        return encryptor.decrypt(encrypted_text)

    def encrypt_config_value(self, plain_text):
        """
        加密配置值，返回 Spring Boot 格式 ENC(...)

        Args:
            plain_text: 待加密的明文

        Returns:
            格式化的密文: ENC(base64密文)
        """
        encrypted = self.encrypt(plain_text)
        return f"ENC({encrypted})"

    def decrypt_config_value(self, config_value):
        """
        解密配置值，支持 ENC(...) 格式或纯密文

        Args:
            config_value: 密文，可以是 "ENC(xxx)" 或 "xxx" 格式

        Returns:
            解密后的明文
        """
        # 提取 ENC() 中的密文
        if config_value.startswith('ENC(') and config_value.endswith(')'):
            encrypted_text = config_value[4:-1]
        else:
            encrypted_text = config_value

        return self.decrypt(encrypted_text)

    @staticmethod
    def get_supported_algorithms():
        """
        获取支持的算法列表

        Returns:
            算法名称列表
        """
        return list(JasyptEncryptor.ALGORITHMS.keys())


# 便捷函数（无需创建实例）
def encrypt(password, plain_text, algorithm='PBEWithMD5AndDES'):
    """
    快捷加密函数

    Args:
        password: 加密密码
        plain_text: 待加密的明文
        algorithm: 加密算法，默认 'PBEWithMD5AndDES'

    Returns:
        Base64 编码的密文
    """
    return JasyptEncryptor.encrypt_with_config(plain_text, password, algorithm)


def decrypt(password, encrypted_text, algorithm='PBEWithMD5AndDES'):
    """
    快捷解密函数

    Args:
        password: 解密密码
        encrypted_text: Base64 编码的密文
        algorithm: 解密算法，默认 'PBEWithMD5AndDES'

    Returns:
        解密后的明文
    """
    return JasyptEncryptor.decrypt_with_config(encrypted_text, password, algorithm)


# 测试代码
if __name__ == '__main__':
    print("=" * 70)
    print("Jasypt 加解密工具类测试")
    print("=" * 70)

    # 测试 1: 验证 Java Jasypt 密文解密
    print("\n【测试 1】解密 Java Jasypt 密文")
    print("-" * 70)

    java_password = 'X7P3L9Q2'
    java_encrypted = '32yI5/lZjpE3byA/wvrPfw=='

    try:
        # 方式 1: 使用静态方法
        decrypted = decrypt(java_password, java_encrypted)
        print(f"✓ 密码: {java_password}")
        print(f"✓ 密文: {java_encrypted}")
        print(f"✓ 明文: {decrypted}")
    except Exception as e:
        print(f"✗ 解密失败: {e}")

    # 测试 2: 使用实例方法
    print("\n【测试 2】使用实例方法加解密")
    print("-" * 70)

    encryptor = JasyptEncryptor(password='mySecretKey')

    test_text = 'Hello Jasypt!'
    encrypted = encryptor.encrypt(test_text)
    decrypted = encryptor.decrypt(encrypted)

    print(f"明文: {test_text}")
    print(f"密文: {encrypted}")
    print(f"解密: {decrypted}")
    print(f"验证: {'✓ 通过' if test_text == decrypted else '✗ 失败'}")

    # 测试 3: Spring Boot 配置格式
    print("\n【测试 3】Spring Boot 配置格式")
    print("-" * 70)

    config_encryptor = JasyptEncryptor(password='springPassword')

    db_password = 'myDbPassword123'
    config_value = config_encryptor.encrypt_config_value(db_password)
    print(f"原始密码: {db_password}")
    print(f"配置格式: spring.datasource.password={config_value}")

    decrypted_value = config_encryptor.decrypt_config_value(config_value)
    print(f"解密结果: {decrypted_value}")
    print(f"验证: {'✓ 通过' if db_password == decrypted_value else '✗ 失败'}")

    # 测试 4: 快捷函数
    print("\n【测试 4】快捷函数测试")
    print("-" * 70)

    password = 'quickTest'
    plain = 'Quick encryption test'

    enc = encrypt(password, plain)
    dec = decrypt(password, enc)

    print(f"明文: {plain}")
    print(f"密文: {enc}")
    print(f"解密: {dec}")
    print(f"验证: {'✓ 通过' if plain == dec else '✗ 失败'}")

    # 测试 5: 不同算法
    print("\n【测试 5】不同算法测试")
    print("-" * 70)

    for algo in JasyptEncryptor.get_supported_algorithms():
        try:
            enc = encrypt('testPass', 'test data', algo)
            dec = decrypt('testPass', enc, algo)
            print(f"✓ {algo}: 加解密成功")
        except Exception as e:
            print(f"✗ {algo}: {e}")

    print("\n" + "=" * 70)
    print("测试完成！")
    print("=" * 70)